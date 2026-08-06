from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Callable

import docker
from docker.errors import APIError, DockerException, NotFound


class DockerRuntimeError(RuntimeError):
    """A Docker daemon or command execution error."""


class ContainerNotFoundError(DockerRuntimeError):
    pass


class ContainerNotRunningError(DockerRuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class TerminalSession:
    exec_id: str
    socket: Any


class DockerRuntime:
    def __init__(
        self,
        client: Any | None = None,
        command_runner: Callable[..., Any] = subprocess.run,
        timeout_seconds: int = 1800,
    ):
        self.client = client if client is not None else docker.from_env()
        self.command_runner = command_runner
        self.timeout_seconds = timeout_seconds

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except DockerException:
            return False

    def list_containers(self) -> list[dict[str, Any]]:
        return [self._serialize_container(container) for container in self.client.containers.list(all=True)]

    def get_container(self, container_id: str) -> Any:
        try:
            return self.client.containers.get(container_id)
        except NotFound as exc:
            raise ContainerNotFoundError(container_id) from exc
        except APIError as exc:
            raise DockerRuntimeError(str(exc)) from exc

    def logs(self, container_id: str, tail: str = "all", timestamps: bool = False) -> bytes:
        container = self.get_container(container_id)
        try:
            output = container.logs(tail=tail, timestamps=timestamps)
        except APIError as exc:
            raise DockerRuntimeError(str(exc)) from exc
        if isinstance(output, str):
            return output.encode()
        return output

    def load_image(self, image_tar: Path, cwd: Path) -> Any:
        return self._run(["docker", "load", "-i", str(image_tar)], cwd)

    def compose_up(self, cwd: Path) -> Any:
        return self._run(
            ["docker", "compose", "--project-directory", str(cwd), "up", "-d"],
            cwd,
        )

    def create_terminal(self, container_id: str, command: list[str]) -> TerminalSession:
        container = self.get_container(container_id)
        state = container.attrs.get("State", {})
        if not state.get("Running", False):
            raise ContainerNotRunningError(container_id)
        try:
            created = self.client.api.exec_create(
                container.id,
                cmd=command,
                stdout=True,
                stderr=True,
                stdin=True,
                tty=True,
            )
            exec_id = created["Id"]
            socket = self.client.api.exec_start(exec_id, tty=True, socket=True)
            return TerminalSession(exec_id=exec_id, socket=socket)
        except APIError as exc:
            raise DockerRuntimeError(str(exc)) from exc

    def resize_terminal(self, exec_id: str, width: int, height: int) -> None:
        try:
            self.client.api.exec_resize(exec_id, height=height, width=width)
        except APIError as exc:
            raise DockerRuntimeError(str(exc)) from exc

    @staticmethod
    def close_terminal(session: TerminalSession) -> None:
        close = getattr(session.socket, "close", None)
        if close is not None:
            close()

    def _run(self, argv: list[str], cwd: Path) -> Any:
        try:
            return self.command_runner(
                argv,
                cwd=str(cwd),
                shell=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerRuntimeError(str(exc)) from exc

    @staticmethod
    def _serialize_container(container: Any) -> dict[str, Any]:
        attrs = container.attrs
        state = attrs.get("State", {})
        image_tags = getattr(getattr(container, "image", None), "tags", []) or []
        image = image_tags[0] if image_tags else attrs.get("Config", {}).get("Image")
        name = getattr(container, "name", None) or attrs.get("Name", "").lstrip("/")
        config = attrs.get("Config", {})
        return {
            "id": container.id,
            "short_id": getattr(container, "short_id", container.id[:12]),
            "name": name,
            "names": [name] if name else [],
            "image": image,
            "image_id": attrs.get("Image"),
            "command": attrs.get("Path") or config.get("Cmd"),
            "created": attrs.get("Created"),
            "status": getattr(container, "status", None),
            "ports": getattr(container, "ports", attrs.get("NetworkSettings", {}).get("Ports")),
            "labels": getattr(container, "labels", config.get("Labels", {})),
            "running": bool(state.get("Running", False)),
            "state": state,
            "mounts": attrs.get("Mounts", []),
            "networks": attrs.get("NetworkSettings", {}).get("Networks", {}),
            "raw_attrs": attrs,
        }
