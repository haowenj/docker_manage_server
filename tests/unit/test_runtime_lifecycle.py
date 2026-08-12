from __future__ import annotations

import pytest

from docker_manage_server.docker_runtime import (
    ComposeProjectRecord,
    ContainerNotFoundError,
)
from docker_manage_server.runtime_inventory import RuntimeInventoryService
from docker_manage_server.runtime_lifecycle import (
    RuntimeActionConflictError,
    RuntimeLifecycleService,
    RuntimeResourceNotFoundError,
)


def container(
    container_id: str,
    *,
    running: bool,
    name: str | None = None,
    project: str | None = None,
):
    labels = {}
    if project:
        labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": "web",
        }
    return {
        "id": container_id,
        "short_id": container_id[:12],
        "name": name or container_id,
        "image": "demo:latest",
        "created": "2026-08-01T00:00:00Z",
        "status": "running" if running else "exited",
        "running": running,
        "ports": {},
        "labels": labels,
        "mounts": [],
        "networks": {},
    }


class FakeRuntime:
    def __init__(self, containers=(), projects=()):
        self.containers = [dict(item) for item in containers]
        self.projects = list(projects)
        self.calls = []

    def list_containers(self):
        return self.containers

    def list_compose_projects(self):
        return tuple(self.projects)

    def get_serialized_container(self, container_id):
        for item in self.containers:
            if item["id"] == container_id or item.get("name") == container_id:
                return dict(item)
        raise ContainerNotFoundError(container_id)

    def start_container(self, container_id):
        self.calls.append(("start_container", container_id))
        item = self._container(container_id)
        item["running"] = True
        item["status"] = "running"

    def stop_container(self, container_id):
        self.calls.append(("stop_container", container_id))
        item = self._container(container_id)
        item["running"] = False
        item["status"] = "exited"

    def restart_container(self, container_id):
        self.calls.append(("restart_container", container_id))

    def remove_container(self, container_id):
        self.calls.append(("remove_container", container_id))
        self.containers.remove(self._container(container_id))

    def start_compose_project(self, name):
        self.calls.append(("start_compose_project", name))
        self._set_project_running(name, True)

    def stop_compose_project(self, name):
        self.calls.append(("stop_compose_project", name))
        self._set_project_running(name, False)

    def restart_compose_project(self, name):
        self.calls.append(("restart_compose_project", name))

    def remove_compose_project(self, name):
        self.calls.append(("remove_compose_project", name))
        self.projects = [item for item in self.projects if item.name != name]
        self.containers = [
            item
            for item in self.containers
            if item.get("labels", {}).get("com.docker.compose.project") != name
        ]

    def _container(self, container_id):
        return next(item for item in self.containers if item["id"] == container_id)

    def _set_project_running(self, name, running):
        status = "running(1)" if running else "exited(1)"
        self.projects = [
            ComposeProjectRecord(item.name, status, item.config_files)
            if item.name == name
            else item
            for item in self.projects
        ]
        for item in self.containers:
            if item.get("labels", {}).get("com.docker.compose.project") == name:
                item["running"] = running
                item["status"] = "running" if running else "exited"


def test_container_actions_use_validated_immutable_id_and_return_latest_state():
    runtime = FakeRuntime(
        containers=[container("immutable", name="alias", running=False)]
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    assert service.start_container("alias")["running"] is True
    assert runtime.calls == [("start_container", "immutable")]


@pytest.mark.parametrize(
    ("method", "running"),
    [
        ("start_container", True),
        ("stop_container", False),
        ("restart_container", False),
        ("remove_container", True),
    ],
)
def test_container_actions_reject_invalid_state(method, running):
    runtime = FakeRuntime(containers=[container("direct", running=running)])
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    with pytest.raises(RuntimeActionConflictError):
        getattr(service, method)("direct")

    assert runtime.calls == []


def test_container_actions_hide_compose_managed_container():
    runtime = FakeRuntime(
        containers=[container("compose-web", running=True, project="mall")]
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    with pytest.raises(ContainerNotFoundError):
        service.stop_container("compose-web")


def test_compose_actions_validate_project_and_return_latest_state():
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", "exited(1)", ())],
        containers=[container("mall-web", running=False, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    assert service.start_compose_project("mall").running is True
    assert runtime.calls == [("start_compose_project", "mall")]


@pytest.mark.parametrize(
    ("method", "status", "running"),
    [
        ("start_compose_project", "running(1)", True),
        ("stop_compose_project", "exited(1)", False),
        ("restart_compose_project", "exited(1)", False),
    ],
)
def test_compose_actions_reject_invalid_state(method, status, running):
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", status, ())],
        containers=[container("mall-web", running=running, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    with pytest.raises(RuntimeActionConflictError):
        getattr(service, method)("mall")

    assert runtime.calls == []


def test_compose_delete_allows_running_and_missing_project_is_hidden():
    runtime = FakeRuntime(
        projects=[ComposeProjectRecord("mall", "running(1)", ())],
        containers=[container("mall-web", running=True, project="mall")],
    )
    service = RuntimeLifecycleService(runtime, RuntimeInventoryService(runtime))

    assert service.remove_compose_project("mall") == {"name": "mall"}
    with pytest.raises(RuntimeResourceNotFoundError):
        service.remove_compose_project("missing")
