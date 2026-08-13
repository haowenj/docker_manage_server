from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import shlex
import socket as socket_module
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.websockets import WebSocketDisconnect

from .artifacts import list_files
from .config import Settings, get_settings
from .deployment import (
    DeploymentConfigurationError,
    DeploymentConfigurationTooLargeError,
    DeploymentService,
    DeploymentStateError,
)
from .deployment_config import (
    can_edit_task,
    can_retry_task,
    effective_directory_rules,
)
from .docker_runtime import (
    ContainerNotFoundError,
    ContainerNotRunningError,
    DockerRuntime,
    DockerRuntimeError,
    ImageNotFoundError,
)
from .image_inventory import (
    ImageInUseError,
    ImageInventoryService,
    InvalidImagePageError,
)
from .models import DeploymentTask, DirectoryRule
from .security import CSP, UNSAFE_METHODS, origin_matches_host
from .storage import TaskStore
from .runtime_inventory import RuntimeInventoryService
from .runtime_lifecycle import (
    RuntimeActionConflictError,
    RuntimeLifecycleService,
    RuntimeResourceNotFoundError,
)
from .web import PACKAGE_ROOT, create_web_router


class DeploymentConfigurationPayload(BaseModel):
    env: str
    compose: str
    directories: tuple[DirectoryRule, ...] = ()


IMAGE_ID_PATTERN = r"^sha256:[0-9a-fA-F]{64}$"


class ImageBatchPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("image_ids")
    @classmethod
    def validate_image_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("image_ids must not contain duplicates")
        if any(re.fullmatch(IMAGE_ID_PATTERN, item) is None for item in value):
            raise ValueError("image_ids must contain full sha256 image IDs")
        return value


class ImageBatchDeletePayload(ImageBatchPreviewPayload):
    query: str = ""
    page: int = Field(default=1, ge=1)


def create_app(
    settings: Settings | None = None,
    store: TaskStore | None = None,
    runtime: DockerRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or TaskStore(settings.data_dir)
    runtime = runtime or DockerRuntime(timeout_seconds=settings.compose_timeout_seconds)
    inventory = RuntimeInventoryService(runtime)
    lifecycle = RuntimeLifecycleService(runtime, inventory)
    images = ImageInventoryService(runtime)
    deployment = DeploymentService(store, runtime)

    app = FastAPI(title="Docker Manage Server", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.runtime = runtime
    app.state.inventory = inventory
    app.state.lifecycle = lifecycle
    app.state.images = images
    app.state.deployment = deployment

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if (
            request.method in UNSAFE_METHODS
            and not origin_matches_host(origin, host)
        ):
            if request.url.path.startswith("/api/"):
                response = JSONResponse(
                    {"detail": "cross-origin request rejected"},
                    status_code=403,
                )
            else:
                response = PlainTextResponse(
                    "cross-origin request rejected",
                    status_code=403,
                )
        else:
            response = await call_next(request)

        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    app.include_router(
        create_web_router(
            store,
            deployment,
            runtime,
            inventory,
            lifecycle,
            images,
        )
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        if not runtime.ping():
            raise HTTPException(status_code=503, detail="docker daemon unavailable")
        try:
            container_count = len(runtime.list_containers())
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "ok", "docker_connected": True, "containers": container_count}

    @app.post("/api/deployment-tasks", status_code=201)
    def upload_deployment(file: UploadFile = File(...)) -> dict[str, Any]:
        task_id = uuid4().hex
        try:
            task = deployment.upload(
                task_id,
                file.file,
                file.filename or "archive.tar.gz",
            )
        except Exception as exc:
            try:
                task = store.get(task_id)
                detail = task.error or str(exc)
            except KeyError:
                detail = str(exc)
            raise HTTPException(status_code=422, detail=detail) from exc
        return _task_payload(task)

    @app.get("/api/deployment-tasks/{task_id}")
    def get_deployment_task(task_id: str) -> dict[str, Any]:
        return _task_payload(_get_task(store, task_id))

    @app.get("/api/deployment-tasks/{task_id}/files")
    def get_deployment_files(task_id: str) -> dict[str, Any]:
        task = _get_task(store, task_id)
        if not task.extracted_dir.is_dir():
            raise HTTPException(status_code=409, detail="task has no extracted files")
        return {"task_id": task_id, "files": [item.model_dump(mode="json") for item in list_files(task.extracted_dir)]}

    @app.get("/api/deployment-tasks/{task_id}/review")
    def review_deployment(task_id: str) -> dict[str, Any]:
        task = _get_task(store, task_id)
        if not task.extracted_dir.is_dir():
            raise HTTPException(status_code=409, detail="task has no extracted files")
        try:
            env_text = (task.extracted_dir / ".env").read_text(encoding="utf-8")
            compose_text = (task.extracted_dir / "compose.yaml").read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "task_id": task_id,
            "app_name": task.app_name,
            "files": [item.model_dump(mode="json") for item in list_files(task.extracted_dir)],
            "env": env_text,
            "compose": compose_text,
            "directories": _directory_payload(task),
            "editable": can_edit_task(task),
            "retryable": can_retry_task(task),
            "edited_at": task.edited_at.isoformat() if task.edited_at else None,
        }

    @app.put("/api/deployment-tasks/{task_id}/configuration")
    def update_deployment_configuration(
        task_id: str,
        payload: DeploymentConfigurationPayload,
    ) -> dict[str, Any]:
        try:
            task = deployment.edit_configuration(
                task_id,
                payload.env,
                payload.compose,
                payload.directories,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="deployment task not found") from exc
        except DeploymentConfigurationTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except DeploymentStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeploymentConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _task_payload(task)

    @app.post("/api/deployment-tasks/{task_id}/deploy", status_code=202)
    def deploy_task(task_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            task = deployment.begin_deploy(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="deployment task not found") from exc
        except DeploymentStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background_tasks.add_task(deployment.deploy, task_id)
        return _task_payload(task)

    @app.delete("/api/deployment-tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, Any]:
        try:
            task = deployment.delete_task(task_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="deployment task not found",
            ) from exc
        except DeploymentStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _task_payload(task)

    @app.get("/api/containers")
    def list_containers() -> dict[str, Any]:
        try:
            return {"items": runtime.list_containers()}
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/images")
    def list_images(q: str = "", page: str = "1") -> dict[str, Any]:
        try:
            return asdict(images.list(q, page))
        except InvalidImagePageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/images/batch-delete-preview")
    def preview_image_batch_delete(
        payload: ImageBatchPreviewPayload,
    ) -> dict[str, Any]:
        try:
            return asdict(
                images.preview_batch_removal(tuple(payload.image_ids))
            )
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/images/batch-delete")
    def delete_image_batch(
        payload: ImageBatchDeletePayload,
    ) -> dict[str, Any]:
        try:
            return asdict(
                images.remove_unused_images(
                    tuple(payload.image_ids),
                    query=payload.query,
                    page=payload.page,
                )
            )
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/images/{image_id}")
    def get_image(image_id: str) -> dict[str, Any]:
        try:
            detail = images.get(image_id)
        except ImageNotFoundError as exc:
            raise HTTPException(status_code=404, detail="image not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "item": asdict(detail.summary),
            "inspect": detail.inspect,
            "containers": [asdict(item) for item in detail.containers],
        }

    @app.get("/api/images/{image_id}/tag-removal-preview")
    def preview_image_tag_removal(image_id: str) -> dict[str, Any]:
        try:
            return asdict(images.preview_tag_removal(image_id))
        except ImageNotFoundError as exc:
            raise HTTPException(status_code=404, detail="image not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.delete("/api/images/{image_id}/tags")
    def remove_image_tags(image_id: str) -> dict[str, Any]:
        try:
            return asdict(images.remove_available_tags(image_id))
        except ImageNotFoundError as exc:
            raise HTTPException(status_code=404, detail="image not found") from exc
        except ImageInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/containers/{container_id}")
    def get_container(container_id: str) -> dict[str, Any]:
        try:
            container = runtime.get_container(container_id)
            return {"item": DockerRuntime._serialize_container(container)}
        except ContainerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="container not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/containers/{container_id}/logs")
    def get_logs(
        container_id: str,
        tail: str = "all",
        timestamps: bool = False,
    ) -> PlainTextResponse:
        try:
            output = runtime.logs(container_id, tail=tail, timestamps=timestamps)
        except ContainerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="container not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return PlainTextResponse(output.decode("utf-8", errors="replace"))

    def container_lifecycle_action(
        container_id: str,
        action: str,
    ) -> dict[str, Any]:
        try:
            item = getattr(lifecycle, f"{action}_container")(container_id)
        except ContainerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="container not found") from exc
        except RuntimeActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"item": item}

    @app.post("/api/containers/{container_id}/start")
    def start_container(container_id: str) -> dict[str, Any]:
        return container_lifecycle_action(container_id, "start")

    @app.post("/api/containers/{container_id}/stop")
    def stop_container(container_id: str) -> dict[str, Any]:
        return container_lifecycle_action(container_id, "stop")

    @app.post("/api/containers/{container_id}/restart")
    def restart_container(container_id: str) -> dict[str, Any]:
        return container_lifecycle_action(container_id, "restart")

    @app.delete("/api/containers/{container_id}")
    def remove_container(container_id: str) -> dict[str, Any]:
        try:
            identity = lifecycle.remove_container(container_id)
        except ContainerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="container not found") from exc
        except RuntimeActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"deleted": True, **identity}

    def compose_lifecycle_action(
        project_name: str,
        action: str,
    ) -> dict[str, Any]:
        try:
            project = getattr(
                lifecycle,
                f"{action}_compose_project",
            )(project_name)
        except RuntimeResourceNotFoundError as exc:
            raise HTTPException(status_code=404, detail="compose project not found") from exc
        except RuntimeActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "item": {
                "name": project.name,
                "status": project.status,
                "running": project.running,
                "container_count": project.container_count,
                "running_containers": project.running_containers,
            }
        }

    @app.post("/api/compose-projects/{project_name}/start")
    def start_compose_project(project_name: str) -> dict[str, Any]:
        return compose_lifecycle_action(project_name, "start")

    @app.post("/api/compose-projects/{project_name}/stop")
    def stop_compose_project(project_name: str) -> dict[str, Any]:
        return compose_lifecycle_action(project_name, "stop")

    @app.post("/api/compose-projects/{project_name}/restart")
    def restart_compose_project(project_name: str) -> dict[str, Any]:
        return compose_lifecycle_action(project_name, "restart")

    @app.delete("/api/compose-projects/{project_name}")
    def remove_compose_project(project_name: str) -> dict[str, Any]:
        try:
            identity = lifecycle.remove_compose_project(project_name)
        except RuntimeResourceNotFoundError as exc:
            raise HTTPException(status_code=404, detail="compose project not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"deleted": True, **identity}

    @app.get("/api/compose-projects/{project_name}/containers/{container_id}/logs")
    def get_compose_logs(
        project_name: str,
        container_id: str,
        tail: str = "all",
        timestamps: bool = False,
    ) -> PlainTextResponse:
        try:
            container = inventory.require_project_container(project_name, container_id)
            output = runtime.logs(
                str(container["id"]), tail=tail, timestamps=timestamps
            )
        except ContainerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="container not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return PlainTextResponse(output.decode("utf-8", errors="replace"))

    @app.websocket("/api/containers/{container_id}/terminal")
    async def terminal(websocket: WebSocket, container_id: str, command: str = "/bin/sh"):
        if not origin_matches_host(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            container = inventory.require_standalone_container(container_id)
        except ContainerNotFoundError:
            await websocket.send_json({"error": "container_not_found"})
            await websocket.close(code=1008)
            return
        except DockerRuntimeError as exc:
            await websocket.send_json({"error": "docker_runtime_error", "detail": str(exc)})
            await websocket.close(code=1011)
            return
        await _serve_terminal(websocket, runtime, str(container["id"]), command)

    @app.websocket(
        "/api/compose-projects/{project_name}/containers/{container_id}/terminal"
    )
    async def compose_terminal(
        websocket: WebSocket,
        project_name: str,
        container_id: str,
        command: str = "/bin/sh",
    ):
        if not origin_matches_host(
            websocket.headers.get("origin"), websocket.headers.get("host")
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            container = inventory.require_project_container(project_name, container_id)
        except ContainerNotFoundError:
            await websocket.send_json({"error": "container_not_found"})
            await websocket.close(code=1008)
            return
        except DockerRuntimeError as exc:
            await websocket.send_json({"error": "docker_runtime_error", "detail": str(exc)})
            await websocket.close(code=1011)
            return
        await _serve_terminal(websocket, runtime, str(container["id"]), command)

    return app


def _get_task(store: TaskStore, task_id: str) -> DeploymentTask:
    try:
        return store.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="deployment task not found") from exc


def _task_payload(task: DeploymentTask) -> dict[str, Any]:
    return task.model_dump(
        mode="json",
        exclude={"created_at", "updated_at"},
    )


def _directory_payload(task: DeploymentTask) -> list[dict[str, Any]]:
    root = task.deployment_dir
    result = []
    for rule in effective_directory_rules(task):
        target = root / rule.path if root is not None else None
        result.append(
            {
                "path": rule.path,
                "mode": rule.mode,
                "exists": bool(
                    target
                    and target.is_dir()
                    and not target.is_symlink()
                ),
            }
        )
    return result


async def _serve_terminal(
    websocket: WebSocket,
    runtime: DockerRuntime,
    container_id: str,
    command: str,
) -> None:
    try:
        session = runtime.create_terminal(container_id, shlex.split(command))
    except ContainerNotFoundError:
        await websocket.send_json({"error": "container_not_found"})
        await websocket.close(code=1008)
        return
    except ContainerNotRunningError:
        await websocket.send_json({"error": "container_not_running"})
        await websocket.close(code=1008)
        return
    except DockerRuntimeError as exc:
        await websocket.send_json({"error": "docker_runtime_error", "detail": str(exc)})
        await websocket.close(code=1011)
        return

    reader = asyncio.create_task(_relay_terminal_output(websocket, session.socket))
    writer = asyncio.create_task(_relay_terminal_input(websocket, runtime, session))
    try:
        await asyncio.wait(
            {reader, writer},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except WebSocketDisconnect:
        pass
    finally:
        runtime.close_terminal(session)
        for task in (reader, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, writer, return_exceptions=True)
        await _close_websocket(websocket)


async def _relay_terminal_output(websocket: WebSocket, socket: Any) -> None:
    raw_socket = _raw_socket(socket)
    if raw_socket is not None:
        raw_socket.setblocking(False)
    while True:
        try:
            if raw_socket is not None:
                chunk = await asyncio.get_running_loop().sock_recv(raw_socket, 4096)
            else:
                chunk = await asyncio.to_thread(_socket_read, socket)
        except TimeoutError:
            continue
        except (OSError, ValueError):
            return
        if not chunk:
            return
        await websocket.send_bytes(chunk)


async def _close_websocket(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _relay_terminal_input(websocket: WebSocket, runtime: DockerRuntime, session: Any) -> None:
    raw_socket = _raw_socket(session.socket)
    if raw_socket is not None:
        raw_socket.setblocking(False)
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            if raw_socket is not None:
                await asyncio.get_running_loop().sock_sendall(raw_socket, data)
            else:
                await asyncio.to_thread(_socket_send, session.socket, data)
            continue
        text = message.get("text")
        if text is None:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await websocket.send_json({"error": "invalid_message"})
            continue
        if payload.get("type") != "resize":
            await websocket.send_json({"error": "unsupported_message"})
            continue
        try:
            width = int(payload["width"])
            height = int(payload["height"])
            if width < 1 or height < 1:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            await websocket.send_json({"error": "invalid_resize"})
            continue
        runtime.resize_terminal(session.exec_id, width, height)


def _socket_send(socket: Any, data: bytes) -> None:
    raw_socket = getattr(socket, "_sock", None)
    raw_sendall = getattr(raw_socket, "sendall", None)
    if raw_sendall is not None:
        raw_sendall(data)
        return
    sendall = getattr(socket, "sendall", None)
    if sendall is not None:
        sendall(data)
        return
    send = getattr(socket, "send", None)
    if send is not None:
        send(data)
        return
    write = getattr(socket, "write", None)
    if write is not None:
        write(data)
        return
    raise TypeError("terminal socket does not support writing")


def _socket_read(socket: Any) -> bytes:
    raw_socket = getattr(socket, "_sock", None)
    raw_recv = getattr(raw_socket, "recv", None)
    if raw_recv is not None:
        return raw_recv(4096)
    recv = getattr(socket, "recv", None)
    if recv is not None:
        return recv(4096)
    read = getattr(socket, "read", None)
    if read is not None:
        return read(4096)
    raise TypeError("terminal socket does not support reading")


def _raw_socket(socket: Any) -> socket_module.socket | None:
    raw_socket = getattr(socket, "_sock", None)
    if isinstance(raw_socket, socket_module.socket):
        return raw_socket
    return None
