from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .deployment import DeploymentService
from .docker_runtime import DockerRuntime, DockerRuntimeError
from .storage import TaskStore
from .web_views import container_view, dashboard_metrics, task_view


PACKAGE_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


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

    return router
