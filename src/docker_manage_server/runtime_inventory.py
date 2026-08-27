from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .docker_runtime import (
    ComposeListError,
    ContainerNotFoundError,
    DockerRuntime,
    DockerRuntimeError,
)


COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
UNREGISTERED_STATUS = "未被 Compose CLI 发现"
PORT_RANGE_START = 6000
PORT_RANGE_END = 9999


@dataclass(frozen=True)
class ComposeProject:
    name: str
    status: str
    config_files: tuple[str, ...]
    containers: tuple[dict[str, Any], ...]

    @property
    def running_containers(self) -> int:
        return sum(bool(item.get("running")) for item in self.containers)

    @property
    def container_count(self) -> int:
        return len(self.containers)

    @property
    def running(self) -> bool:
        return self.running_containers > 0 or self.status.casefold().startswith(
            "running"
        )


@dataclass(frozen=True)
class RuntimeOverview:
    compose_projects: tuple[ComposeProject, ...] = ()
    standalone_containers: tuple[dict[str, Any], ...] = ()
    compose_error: str | None = None
    docker_error: str | None = None


@dataclass(frozen=True)
class PortStatus:
    number: int
    mapped: bool

    @property
    def available(self) -> bool:
        return not self.mapped


@dataclass(frozen=True)
class PortOverview:
    start: int
    end: int
    ports: tuple[PortStatus, ...]

    @property
    def total_count(self) -> int:
        return len(self.ports)

    @property
    def mapped_ports(self) -> tuple[int, ...]:
        return tuple(port.number for port in self.ports if port.mapped)

    @property
    def available_ports(self) -> tuple[int, ...]:
        return tuple(port.number for port in self.ports if port.available)

    @property
    def mapped_count(self) -> int:
        return len(self.mapped_ports)

    @property
    def available_count(self) -> int:
        return len(self.available_ports)


def docker_host_ports(
    containers: Iterable[Mapping[str, Any]],
) -> set[int]:
    mapped = set()
    for container in containers:
        ports = container.get("ports")
        if not isinstance(ports, Mapping):
            continue
        for bindings in ports.values():
            if isinstance(bindings, Mapping):
                entries: Sequence[object] = (bindings,)
            elif isinstance(bindings, Sequence) and not isinstance(
                bindings, (str, bytes)
            ):
                entries = bindings
            else:
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                raw_port = entry.get("HostPort")
                if isinstance(raw_port, bool):
                    continue
                try:
                    port = int(raw_port)
                except (TypeError, ValueError):
                    continue
                if 1 <= port <= 65535:
                    mapped.add(port)
    return mapped


def build_port_overview(
    containers: Iterable[Mapping[str, Any]],
    start: int = PORT_RANGE_START,
    end: int = PORT_RANGE_END,
) -> PortOverview:
    mapped = docker_host_ports(containers)
    ports = tuple(
        PortStatus(number=number, mapped=number in mapped)
        for number in range(start, end + 1)
    )
    return PortOverview(start=start, end=end, ports=ports)


def filter_runtime_overview(
    overview: RuntimeOverview,
    compose_query: str = "",
    container_query: str = "",
) -> RuntimeOverview:
    compose_query = compose_query.strip().casefold()
    container_query = container_query.strip().casefold()

    if compose_query:
        projects = []
        for project in overview.compose_projects:
            filtered = _filter_compose_project(project, compose_query)
            if filtered is not None:
                projects.append(filtered)
        compose_projects = tuple(projects)
    else:
        compose_projects = overview.compose_projects

    if container_query:
        standalone_containers = tuple(
            item
            for item in overview.standalone_containers
            if _matches_any(
                container_query,
                item.get("name"),
                item.get("id"),
                item.get("short_id"),
                item.get("image"),
            )
        )
    else:
        standalone_containers = overview.standalone_containers

    return RuntimeOverview(
        compose_projects=compose_projects,
        standalone_containers=standalone_containers,
        compose_error=overview.compose_error,
        docker_error=overview.docker_error,
    )


def _filter_compose_project(
    project: ComposeProject,
    query: str,
) -> ComposeProject | None:
    if _matches_any(query, project.name):
        return project

    containers = tuple(
        item
        for item in project.containers
        if _matches_any(
            query,
            item.get("name"),
            item.get("id"),
            item.get("short_id"),
            item.get("compose_service"),
        )
    )
    if not containers:
        return None
    return ComposeProject(
        name=project.name,
        status=project.status,
        config_files=project.config_files,
        containers=containers,
    )


def _matches_any(query: str, *values: object) -> bool:
    return any(
        query in str(value).casefold()
        for value in values
        if value is not None
    )


class RuntimeInventoryService:
    def __init__(self, runtime: DockerRuntime):
        self.runtime = runtime

    def load(self) -> RuntimeOverview:
        try:
            containers = self.runtime.list_containers()
        except DockerRuntimeError as exc:
            return RuntimeOverview(docker_error=str(exc))

        compose_error = None
        try:
            records = self.runtime.list_compose_projects()
        except ComposeListError as exc:
            records = ()
            compose_error = str(exc)

        projects: dict[str, dict[str, Any]] = {
            record.name: {
                "status": record.status,
                "config_files": record.config_files,
                "containers": [],
            }
            for record in records
        }
        standalone = []
        for item in containers:
            labels = _labels(item)
            project_name = labels.get(COMPOSE_PROJECT_LABEL)
            if not isinstance(project_name, str) or not project_name:
                standalone.append(item)
                continue
            entry = projects.setdefault(
                project_name,
                {
                    "status": UNREGISTERED_STATUS,
                    "config_files": _split_config_files(
                        labels.get(COMPOSE_CONFIG_FILES_LABEL)
                    ),
                    "containers": [],
                },
            )
            entry["containers"].append(_enrich_container(item, project_name))

        compose_projects = tuple(
            sorted(
                (
                    ComposeProject(
                        name=name,
                        status=value["status"],
                        config_files=tuple(value["config_files"]),
                        containers=tuple(value["containers"]),
                    )
                    for name, value in projects.items()
                ),
                key=lambda project: (not project.running, project.name.casefold()),
            )
        )
        standalone.sort(key=lambda item: str(item.get("created") or ""), reverse=True)
        standalone.sort(key=lambda item: not bool(item.get("running")))
        return RuntimeOverview(
            compose_projects=compose_projects,
            standalone_containers=tuple(standalone),
            compose_error=compose_error,
        )

    def find_project(self, name: str) -> ComposeProject | None:
        overview = self.load()
        if overview.docker_error:
            raise DockerRuntimeError(overview.docker_error)
        if overview.compose_error:
            raise ComposeListError(overview.compose_error)
        return next(
            (project for project in overview.compose_projects if project.name == name),
            None,
        )

    def require_project_container(
        self, project_name: str, container_id: str
    ) -> dict[str, Any]:
        item = self.runtime.get_serialized_container(container_id)
        labels = _labels(item)
        if labels.get(COMPOSE_PROJECT_LABEL) != project_name:
            raise ContainerNotFoundError(container_id)
        return _enrich_container(item, project_name)

    def require_standalone_container(self, container_id: str) -> dict[str, Any]:
        item = self.runtime.get_serialized_container(container_id)
        if _labels(item).get(COMPOSE_PROJECT_LABEL):
            raise ContainerNotFoundError(container_id)
        return item


def _labels(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("labels")
    return value if isinstance(value, dict) else {}


def _split_config_files(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )


def _enrich_container(
    item: dict[str, Any], project_name: str
) -> dict[str, Any]:
    labels = _labels(item)
    enriched = dict(item)
    enriched["compose_project"] = project_name
    enriched["compose_service"] = labels.get(COMPOSE_SERVICE_LABEL) or "—"
    return enriched
