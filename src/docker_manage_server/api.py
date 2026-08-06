from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import PlainTextResponse
from starlette.websockets import WebSocketDisconnect

from .artifacts import list_files
from .config import Settings, get_settings
from .deployment import DeploymentService, DeploymentStateError
from .docker_runtime import (
    ContainerNotFoundError,
    ContainerNotRunningError,
    DockerRuntime,
    DockerRuntimeError,
)
from .models import DeploymentTask, TaskStatus
from .storage import TaskStore


def create_app(
    settings: Settings | None = None,
    store: TaskStore | None = None,
    runtime: DockerRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or TaskStore(settings.data_dir)
    runtime = runtime or DockerRuntime(timeout_seconds=settings.compose_timeout_seconds)
    deployment = DeploymentService(store, runtime)

    app = FastAPI(title="Docker Manage Server", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.runtime = runtime
    app.state.deployment = deployment

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
        }

    @app.post("/api/deployment-tasks/{task_id}/deploy", status_code=202)
    def deploy_task(task_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        task = _get_task(store, task_id)
        if task.status is not TaskStatus.PENDING_REVIEW:
            raise HTTPException(status_code=409, detail="task is not pending review")
        background_tasks.add_task(deployment.deploy, task_id)
        return _task_payload(task.model_copy(update={"status": TaskStatus.DEPLOYING}))

    @app.delete("/api/deployment-tasks/{task_id}")
    def discard_task(task_id: str) -> dict[str, Any]:
        try:
            task = deployment.discard(task_id)
        except DeploymentStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_payload(task)

    @app.get("/api/containers")
    def list_containers() -> dict[str, Any]:
        try:
            return {"items": runtime.list_containers()}
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

    @app.websocket("/api/containers/{container_id}/terminal")
    async def terminal(websocket: WebSocket, container_id: str, command: str = "/bin/sh"):
        await websocket.accept()
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
            for task in (reader, writer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)
            runtime.close_terminal(session)

    return app


def _get_task(store: TaskStore, task_id: str) -> DeploymentTask:
    try:
        return store.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="deployment task not found") from exc


def _task_payload(task: DeploymentTask) -> dict[str, Any]:
    return task.model_dump(mode="json")


async def _relay_terminal_output(websocket: WebSocket, socket: Any) -> None:
    while True:
        chunk = await asyncio.to_thread(socket.recv, 4096)
        if not chunk:
            return
        await websocket.send_bytes(chunk)


async def _relay_terminal_input(websocket: WebSocket, runtime: DockerRuntime, session: Any) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
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
    sendall = getattr(socket, "sendall", None)
    if sendall is not None:
        sendall(data)
    else:
        socket.send(data)
