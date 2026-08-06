from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile

from .artifacts import list_files
from .config import Settings, get_settings
from .deployment import DeploymentService, DeploymentStateError
from .docker_runtime import DockerRuntime, DockerRuntimeError
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

    return app


def _get_task(store: TaskStore, task_id: str) -> DeploymentTask:
    try:
        return store.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="deployment task not found") from exc


def _task_payload(task: DeploymentTask) -> dict[str, Any]:
    return task.model_dump(mode="json")
