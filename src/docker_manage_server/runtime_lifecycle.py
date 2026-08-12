from __future__ import annotations

from typing import Any, Callable

from .docker_runtime import DockerRuntime
from .runtime_inventory import ComposeProject, RuntimeInventoryService


class RuntimeActionConflictError(RuntimeError):
    pass


class RuntimeResourceNotFoundError(RuntimeError):
    pass


class RuntimeLifecycleService:
    def __init__(
        self,
        runtime: DockerRuntime,
        inventory: RuntimeInventoryService,
    ):
        self.runtime = runtime
        self.inventory = inventory

    def start_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(
            container_id,
            "start",
            expected_running=False,
        )

    def stop_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(
            container_id,
            "stop",
            expected_running=True,
        )

    def restart_container(self, container_id: str) -> dict[str, Any]:
        return self._container_action(
            container_id,
            "restart",
            expected_running=True,
        )

    def remove_container(self, container_id: str) -> dict[str, Any]:
        item = self.inventory.require_standalone_container(container_id)
        if item.get("running"):
            raise RuntimeActionConflictError("运行中的独立容器必须先停止")
        immutable_id = str(item["id"])
        self.runtime.remove_container(immutable_id)
        return {
            "id": immutable_id,
            "name": item.get("name") or immutable_id,
        }

    def _container_action(
        self,
        container_id: str,
        action: str,
        expected_running: bool,
    ) -> dict[str, Any]:
        item = self.inventory.require_standalone_container(container_id)
        if bool(item.get("running")) is not expected_running:
            raise RuntimeActionConflictError("容器当前状态不允许此操作")
        immutable_id = str(item["id"])
        method: Callable[[str], None] = getattr(
            self.runtime,
            f"{action}_container",
        )
        method(immutable_id)
        return self.inventory.require_standalone_container(immutable_id)

    def start_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(
            name,
            "start",
            expected_running=False,
        )

    def stop_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(
            name,
            "stop",
            expected_running=True,
        )

    def restart_compose_project(self, name: str) -> ComposeProject:
        return self._compose_action(
            name,
            "restart",
            expected_running=True,
        )

    def remove_compose_project(self, name: str) -> dict[str, str]:
        self._require_project(name)
        self.runtime.remove_compose_project(name)
        return {"name": name}

    def _compose_action(
        self,
        name: str,
        action: str,
        expected_running: bool,
    ) -> ComposeProject:
        project = self._require_project(name)
        if project.running is not expected_running:
            raise RuntimeActionConflictError(
                "Compose 项目当前状态不允许此操作"
            )
        method: Callable[[str], None] = getattr(
            self.runtime,
            f"{action}_compose_project",
        )
        method(name)
        return self._require_project(name)

    def _require_project(self, name: str) -> ComposeProject:
        project = self.inventory.find_project(name)
        if project is None:
            raise RuntimeResourceNotFoundError(name)
        return project
