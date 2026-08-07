from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .artifacts import list_files
from .deployment import DeploymentService, DeploymentStateError
from .docker_runtime import DockerRuntime, DockerRuntimeError
from .models import TaskStatus
from .storage import TaskStore
from .web_views import container_view, dashboard_metrics, task_view


PACKAGE_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def _deployment_list_context(store: TaskStore) -> dict[str, Any]:
    return {
        "page_title": "部署任务",
        "active_nav": "deployments",
        "tasks": [task_view(task) for task in store.list()],
        "upload_error": None,
    }


def _web_error(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="errors/error.html",
        context={
            "page_title": title,
            "active_nav": None,
            "status_code": status_code,
            "error_title": title,
            "error_detail": detail,
        },
        status_code=status_code,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def create_web_router(
    store: TaskStore,
    deployment: DeploymentService,
    runtime: DockerRuntime,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        tasks = store.list()
        docker_error = None
        try:
            containers = runtime.list_containers()
        except DockerRuntimeError as exc:
            containers = []
            docker_error = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "运行概览",
                "active_nav": "dashboard",
                "tasks": [task_view(task) for task in tasks[:5]],
                "containers": [container_view(item) for item in containers[:5]],
                "metrics": dashboard_metrics(tasks, containers),
                "docker_error": docker_error,
            },
        )

    @router.get("/deployments", response_class=HTMLResponse)
    def deployments_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="deployments/list.html",
            context=_deployment_list_context(store),
        )

    @router.post("/deployments", response_class=HTMLResponse)
    def upload_archive(request: Request, file: UploadFile = File(...)):
        task_id = uuid4().hex
        try:
            deployment.upload(task_id, file.file, file.filename or "archive.tar.gz")
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="deployments/list.html",
                context={
                    **_deployment_list_context(store),
                    "upload_error": f"归档校验失败：{exc}",
                },
                status_code=422,
            )
        return RedirectResponse(f"/deployments/{task_id}", status_code=303)

    @router.get("/deployments/{task_id}", response_class=HTMLResponse)
    def deployment_detail(request: Request, task_id: str):
        try:
            task = store.get(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        files = list_files(task.extracted_dir) if task.extracted_dir.is_dir() else ()
        env_text = _read_optional(task.extracted_dir / ".env")
        compose_text = _read_optional(task.extracted_dir / "compose.yaml")
        return templates.TemplateResponse(
            request=request,
            name="deployments/detail.html",
            context={
                "page_title": task.app_name or task.original_filename,
                "active_nav": "deployments",
                "task_view": task_view(task),
                "files": files,
                "env_text": env_text,
                "compose_text": compose_text,
            },
        )

    @router.post("/deployments/{task_id}/deploy")
    def deploy_archive(
        request: Request,
        task_id: str,
        background_tasks: BackgroundTasks,
    ):
        try:
            task = store.get(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        if task.status is not TaskStatus.PENDING_REVIEW:
            return _web_error(request, 409, "任务当前状态不允许部署", task.status.value)
        background_tasks.add_task(deployment.deploy, task_id)
        return RedirectResponse(f"/deployments/{task_id}", status_code=303)

    @router.post("/deployments/{task_id}/discard")
    def discard_archive(request: Request, task_id: str):
        try:
            deployment.discard(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        except DeploymentStateError as exc:
            return _web_error(request, 409, "任务当前状态不允许丢弃", str(exc))
        return RedirectResponse("/deployments", status_code=303)

    return router
