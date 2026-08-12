from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable

import docker
from docker.errors import APIError, DockerException, NotFound


class DockerRuntimeError(RuntimeError):
    """A Docker daemon or command execution error."""


class ComposeListError(DockerRuntimeError):
    pass


class ContainerNotFoundError(DockerRuntimeError):
    pass


class ContainerNotRunningError(DockerRuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class ComposeProjectRecord:
    name: str
    status: str
    config_files: tuple[str, ...]


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
        try:
            containers = self.client.containers.list(all=True)
        except DockerException as exc:
            raise DockerRuntimeError(str(exc)) from exc
        return [self._serialize_container(container) for container in containers]

    def list_compose_projects(self) -> tuple[ComposeProjectRecord, ...]:
        try:
            result = self._run(
                ["docker", "compose", "ls", "--all", "--format", "json"],
                Path.cwd(),
            )
        except DockerRuntimeError as exc:
            raise ComposeListError(str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ComposeListError(
                detail or f"docker compose ls exited {result.returncode}"
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8") or "[]")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComposeListError(f"invalid docker compose ls JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise ComposeListError("invalid docker compose ls JSON: expected a list")
        records = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("Name"), str):
                raise ComposeListError("invalid docker compose ls project record")
            records.append(
                ComposeProjectRecord(
                    name=item["Name"],
                    status=str(item.get("Status") or "unknown"),
                    config_files=_config_files(item.get("ConfigFiles")),
                )
            )
        return tuple(records)

    def get_container(self, container_id: str) -> Any:
        try:
            return self.client.containers.get(container_id)
        except NotFound as exc:
            raise ContainerNotFoundError(container_id) from exc
        except APIError as exc:
            raise DockerRuntimeError(str(exc)) from exc

    def get_serialized_container(self, container_id: str) -> dict[str, Any]:
        return self._serialize_container(self.get_container(container_id))

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

    def compose_config(
        self,
        project_dir: Path,
        compose_file: Path,
        env_file: Path,
    ) -> Any:
        return self._run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(project_dir),
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "config",
                "--quiet",
            ],
            project_dir,
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


def _config_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )
