from __future__ import annotations

import pytest

from docker_manage_server.docker_runtime import (
    ComposeListError,
    ComposeProjectRecord,
    ContainerNotFoundError,
    DockerRuntimeError,
)
from docker_manage_server.runtime_inventory import (
    RuntimeInventoryService,
    RuntimeOverview,
    filter_runtime_overview,
)


def container(
    container_id: str,
    *,
    running: bool,
    created: str,
    project: str | None = None,
    service: str | None = None,
    config_files: str | None = None,
):
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service or "web"
    if config_files:
        labels["com.docker.compose.project.config_files"] = config_files
    return {
        "id": container_id,
        "short_id": container_id,
        "name": container_id,
        "created": created,
        "running": running,
        "status": "running" if running else "exited",
        "labels": labels,
        "ports": {},
        "mounts": [],
        "networks": {},
    }


class FakeRuntime:
    def __init__(self, projects=(), containers=()):
        self.projects = projects
        self.containers = list(containers)

    def list_compose_projects(self):
        return self.projects

    def list_containers(self):
        return self.containers

    def get_serialized_container(self, container_id):
        for item in self.containers:
            if item["id"] == container_id:
                return item
        raise ContainerNotFoundError(container_id)


def test_inventory_groups_compose_and_sorts_standalone_containers():
    runtime = FakeRuntime(
        projects=(
            ComposeProjectRecord(
                "stopped-empty", "exited(0)", ("/srv/empty/compose.yaml",)
            ),
            ComposeProjectRecord(
                "alpha", "running(1)", ("/srv/alpha/compose.yaml",)
            ),
        ),
        containers=[
            container(
                "old-running", running=True, created="2026-01-01T00:00:00Z"
            ),
            container(
                "alpha-web",
                running=True,
                created="2026-03-01T00:00:00Z",
                project="alpha",
            ),
            container(
                "new-running", running=True, created="2026-02-01T00:00:00Z"
            ),
            container(
                "new-stopped", running=False, created="2026-04-01T00:00:00Z"
            ),
        ],
    )

    overview = RuntimeInventoryService(runtime).load()

    assert [project.name for project in overview.compose_projects] == [
        "alpha",
        "stopped-empty",
    ]
    assert [item["id"] for item in overview.standalone_containers] == [
        "new-running",
        "old-running",
        "new-stopped",
    ]
    assert overview.compose_projects[0].containers[0]["compose_service"] == "web"
    assert overview.compose_projects[1].container_count == 0


def test_inventory_uses_labels_when_compose_cli_fails():
    runtime = FakeRuntime(
        containers=[
            container(
                "orphan-web",
                running=False,
                created="2026-01-01T00:00:00Z",
                project="orphan",
                config_files="/srv/orphan/compose.yaml,/srv/orphan/compose.prod.yaml",
            ),
            container("direct", running=True, created="2026-02-01T00:00:00Z"),
        ],
    )

    def fail():
        raise ComposeListError("compose plugin unavailable")

    runtime.list_compose_projects = fail
    overview = RuntimeInventoryService(runtime).load()

    assert overview.compose_error == "compose plugin unavailable"
    assert [project.name for project in overview.compose_projects] == ["orphan"]
    assert overview.compose_projects[0].status == "未被 Compose CLI 发现"
    assert overview.compose_projects[0].config_files == (
        "/srv/orphan/compose.yaml",
        "/srv/orphan/compose.prod.yaml",
    )
    assert [item["id"] for item in overview.standalone_containers] == ["direct"]


def test_inventory_reports_docker_failure_without_compose_lookup():
    runtime = FakeRuntime()

    def fail_containers():
        raise DockerRuntimeError("daemon offline")

    def unexpected_compose_lookup():
        raise AssertionError("compose lookup must not run when Docker is offline")

    runtime.list_containers = fail_containers
    runtime.list_compose_projects = unexpected_compose_lookup

    overview = RuntimeInventoryService(runtime).load()

    assert overview.docker_error == "daemon offline"
    assert overview.compose_projects == ()
    assert overview.standalone_containers == ()


def test_require_project_and_standalone_container_enforce_labels():
    runtime = FakeRuntime(
        containers=[
            container(
                "compose-web",
                running=True,
                created="2026-01-01T00:00:00Z",
                project="mall",
            ),
            container("direct", running=True, created="2026-01-01T00:00:00Z"),
        ]
    )
    inventory = RuntimeInventoryService(runtime)

    assert inventory.require_project_container("mall", "compose-web")["id"] == (
        "compose-web"
    )
    assert inventory.require_standalone_container("direct")["id"] == "direct"

    for project_name, container_id in (
        ("other", "compose-web"),
        ("mall", "direct"),
    ):
        with pytest.raises(ContainerNotFoundError):
            inventory.require_project_container(project_name, container_id)

    with pytest.raises(ContainerNotFoundError):
        inventory.require_standalone_container("compose-web")


def test_find_project_returns_project_or_none_and_maps_runtime_failures():
    runtime = FakeRuntime(
        projects=(ComposeProjectRecord("mall", "running(0)", ()),)
    )
    inventory = RuntimeInventoryService(runtime)

    assert inventory.find_project("mall").name == "mall"
    assert inventory.find_project("missing") is None

    runtime.list_containers = lambda: (_ for _ in ()).throw(
        DockerRuntimeError("daemon offline")
    )
    with pytest.raises(DockerRuntimeError, match="daemon offline"):
        inventory.find_project("mall")

    runtime.list_containers = lambda: []
    runtime.list_compose_projects = lambda: (_ for _ in ()).throw(
        ComposeListError("compose plugin unavailable")
    )
    with pytest.raises(DockerRuntimeError, match="compose plugin unavailable"):
        inventory.find_project("mall")


def runtime_overview_for_query_tests():
    runtime = FakeRuntime(
        projects=(
            ComposeProjectRecord(
                "Mall", "running(2)", ("/srv/mall/compose.yaml",)
            ),
        ),
        containers=[
            container(
                "mall-web-full-id",
                running=True,
                created="2026-08-01T00:00:00Z",
                project="Mall",
                service="web",
            )
            | {"name": "mall-web", "image": "nginx:1.27"},
            container(
                "mall-worker-full-id",
                running=True,
                created="2026-08-01T00:00:00Z",
                project="Mall",
                service="worker",
            )
            | {"name": "mall-worker", "image": "python:3.13"},
            container(
                "standalone-full-id",
                running=True,
                created="2026-08-02T00:00:00Z",
            )
            | {
                "name": "utility",
                "short_id": "standalone",
                "image": "alpine:3.21",
            },
        ],
    )
    return RuntimeInventoryService(runtime).load()


def test_filter_runtime_overview_matches_compose_project_name_and_keeps_all_project_containers():
    overview = runtime_overview_for_query_tests()

    filtered = filter_runtime_overview(overview, compose_query="mAlL")

    assert [project.name for project in filtered.compose_projects] == ["Mall"]
    assert [item["name"] for item in filtered.compose_projects[0].containers] == [
        "mall-web",
        "mall-worker",
    ]
    assert filtered.standalone_containers == overview.standalone_containers


def test_filter_runtime_overview_matches_compose_container_name_or_service_only():
    overview = runtime_overview_for_query_tests()

    filtered = filter_runtime_overview(overview, compose_query="WORK")

    assert [item["name"] for item in filtered.compose_projects[0].containers] == [
        "mall-worker",
    ]


def test_filter_runtime_overview_matches_standalone_name_id_or_image_and_keeps_queries_separate():
    overview = runtime_overview_for_query_tests()

    by_short_id = filter_runtime_overview(overview, container_query="STANDALONE")
    by_image = filter_runtime_overview(overview, container_query="ALPINE")
    by_name = filter_runtime_overview(overview, container_query="utility")

    assert [item["name"] for item in by_short_id.standalone_containers] == [
        "utility"
    ]
    assert [item["name"] for item in by_image.standalone_containers] == ["utility"]
    assert [item["name"] for item in by_name.standalone_containers] == ["utility"]
    assert [project.name for project in by_name.compose_projects] == ["Mall"]


def test_filter_runtime_overview_ignores_whitespace_only_queries_and_preserves_errors():
    overview = runtime_overview_for_query_tests()
    overview = RuntimeOverview(
        compose_projects=overview.compose_projects,
        standalone_containers=overview.standalone_containers,
        compose_error="compose unavailable",
        docker_error=None,
    )

    filtered = filter_runtime_overview(
        overview, compose_query="  ", container_query=""
    )

    assert filtered.compose_projects == overview.compose_projects
    assert filtered.standalone_containers == overview.standalone_containers
    assert filtered.compose_error == "compose unavailable"
