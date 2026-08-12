from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .artifacts import list_files
from .deployment import (
    DeploymentConfigurationError,
    DeploymentConfigurationTooLargeError,
    DeploymentService,
    DeploymentStateError,
)
from .deployment_config import (
    can_edit_task,
    effective_directory_rules,
)
from .docker_runtime import ContainerNotFoundError, DockerRuntime, DockerRuntimeError
from .models import DirectoryRule
from .storage import TaskStore
from .runtime_inventory import RuntimeInventoryService
from .web_views import (
    compose_project_view,
    container_view,
    runtime_metrics,
    task_view,
)


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


def _directory_views(task) -> list[dict[str, Any]]:
    root = task.deployment_dir
    rows = []
    for rule in effective_directory_rules(task):
        target = root / rule.path if root is not None else None
        rows.append(
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
    return rows


def _configuration_context(
    task,
    env_text: str | None = None,
    compose_text: str | None = None,
    directories: list[dict[str, Any]] | None = None,
    edit_error: str | None = None,
) -> dict[str, Any]:
    if directories is None:
        directories = [
            rule.model_dump(mode="json")
            for rule in effective_directory_rules(task)
        ]
    return {
        "page_title": f"编辑 {task.app_name or task.original_filename}",
        "active_nav": "deployments",
        "task_view": task_view(task),
        "env_text": (
            _read_optional(task.extracted_dir / ".env")
            if env_text is None
            else env_text
        ),
        "compose_text": (
            _read_optional(task.extracted_dir / "compose.yaml")
            if compose_text is None
            else compose_text
        ),
        "directories": directories,
        "edit_error": edit_error,
    }


def _container_page(
    request: Request,
    runtime: DockerRuntime,
    container_id: str,
    template_name: str,
) -> HTMLResponse:
    try:
        item = DockerRuntime._serialize_container(runtime.get_container(container_id))
    except ContainerNotFoundError:
        return _web_error(request, 404, "找不到容器", container_id)
    except DockerRuntimeError as exc:
        return _web_error(request, 503, "Docker daemon 不可用", str(exc))
    container = container_view(item)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "page_title": container["name"] or container["item"].get("short_id"),
            "active_nav": "containers",
            "container": container,
        },
    )


def create_web_router(
    store: TaskStore,
    deployment: DeploymentService,
    runtime: DockerRuntime,
    inventory: RuntimeInventoryService,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        tasks = store.list()
        overview = inventory.load()
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "运行概览",
                "active_nav": "dashboard",
                "tasks": [task_view(task) for task in tasks[:5]],
                "compose_projects": [
                    compose_project_view(project)
                    for project in overview.compose_projects[:5]
                ],
                "standalone_containers": [
                    container_view(item)
                    for item in overview.standalone_containers[:5]
                ],
                "metrics": runtime_metrics(tasks, overview),
                "compose_error": overview.compose_error,
                "docker_error": overview.docker_error,
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
                "directories": _directory_views(task),
            },
        )

    @router.get("/deployments/{task_id}/edit", response_class=HTMLResponse)
    def edit_deployment(request: Request, task_id: str):
        try:
            task = store.get(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        if not can_edit_task(task):
            return _web_error(request, 409, "任务当前状态不允许编辑", task.status.value)
        return templates.TemplateResponse(
            request=request,
            name="deployments/edit.html",
            context=_configuration_context(task),
        )

    @router.post("/deployments/{task_id}/edit", response_class=HTMLResponse)
    def save_deployment_edit(
        request: Request,
        task_id: str,
        env: str = Form(...),
        compose: str = Form(...),
        directories_json: str = Form("[]"),
    ):
        raw: Any = []
        try:
            task = store.get(task_id)
            raw = json.loads(directories_json)
            if not isinstance(raw, list):
                raise ValueError("directories must be a list")
            directories = tuple(DirectoryRule.model_validate(item) for item in raw)
            deployment.edit_configuration(task_id, env, compose, directories)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        except DeploymentStateError as exc:
            return _web_error(request, 409, "任务当前状态不允许编辑", str(exc))
        except DeploymentConfigurationTooLargeError as exc:
            return templates.TemplateResponse(
                request=request,
                name="deployments/edit.html",
                context=_configuration_context(task, env, compose, raw, str(exc)),
                status_code=413,
            )
        except (ValueError, ValidationError, DeploymentConfigurationError) as exc:
            submitted = raw if isinstance(raw, list) else []
            return templates.TemplateResponse(
                request=request,
                name="deployments/edit.html",
                context=_configuration_context(task, env, compose, submitted, str(exc)),
                status_code=422,
            )
        return RedirectResponse(f"/deployments/{task_id}", status_code=303)

    @router.post("/deployments/{task_id}/deploy")
    def deploy_archive(
        request: Request,
        task_id: str,
        background_tasks: BackgroundTasks,
    ):
        try:
            deployment.begin_deploy(task_id)
        except KeyError:
            return _web_error(request, 404, "找不到部署任务", task_id)
        except DeploymentStateError as exc:
            return _web_error(request, 409, "任务当前状态不允许部署", str(exc))
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

    @router.get("/runtime", response_class=HTMLResponse)
    def runtime_page(request: Request):
        overview = inventory.load()
        if overview.docker_error:
            return _web_error(
                request, 503, "Docker daemon 不可用", overview.docker_error
            )
        return templates.TemplateResponse(
            request=request,
            name="runtime/list.html",
            context={
                "page_title": "运行管理",
                "active_nav": "runtime",
                "compose_projects": [
                    compose_project_view(project)
                    for project in overview.compose_projects
                ],
                "standalone_containers": [
                    container_view(item) for item in overview.standalone_containers
                ],
                "compose_error": overview.compose_error,
            },
        )

    @router.get("/containers")
    def legacy_container_list():
        return RedirectResponse("/runtime", status_code=307)

    @router.get("/containers/{container_id}", response_class=HTMLResponse)
    def container_detail(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/detail.html"
        )

    @router.get("/containers/{container_id}/logs", response_class=HTMLResponse)
    def container_logs(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/logs.html"
        )

    @router.get("/containers/{container_id}/terminal", response_class=HTMLResponse)
    def container_terminal(request: Request, container_id: str):
        return _container_page(
            request, runtime, container_id, "containers/terminal.html"
        )

    return router
