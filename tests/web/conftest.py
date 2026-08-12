from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import (
    ComposeListError,
    ContainerNotFoundError,
    ImageNotFoundError,
)
from docker_manage_server.storage import TaskStore


class WebFakeRuntime:
    def __init__(self):
        self.available = True
        self.compose_config_returncode = 0
        self.compose_config_stderr = b""
        self.compose_error = None
        self.compose_projects = ()
        self.lifecycle_calls = []
        self.images = []
        self.image_remove_calls = []
        self.image_error = None
        self.containers = [
            {
                "id": "abc123",
                "short_id": "abc123",
                "name": "server",
                "image": "demo/server:1",
                "status": "running",
                "running": True,
                "ports": {"8000/tcp": [{"HostPort": "6308"}]},
                "labels": {},
                "mounts": [],
                "networks": {},
                "raw_attrs": {"State": {"Running": True}},
            }
        ]

    def ping(self):
        return self.available

    def list_containers(self):
        if not self.available:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError("daemon offline")
        return self.containers

    def list_compose_projects(self):
        if self.compose_error:
            raise ComposeListError(self.compose_error)
        return self.compose_projects

    def list_images(self):
        if self.image_error:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError(self.image_error)
        return [dict(item) for item in self.images]

    def get_serialized_image(self, reference):
        if self.image_error:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError(self.image_error)
        for item in self.images:
            if reference in (
                item["id"],
                item.get("short_id"),
                *item.get("tags", ()),
            ):
                return dict(item)
        raise ImageNotFoundError(reference)

    def remove_image(self, reference):
        if self.image_error:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError(self.image_error)
        self.image_remove_calls.append(reference)
        for item in list(self.images):
            if reference in item.get("tags", ()):
                item["tags"].remove(reference)
                if not item["tags"]:
                    self.images.remove(item)
                return
            if reference == item["id"]:
                self.images.remove(item)
                return
        raise ImageNotFoundError(reference)

    def get_serialized_container(self, container_id):
        for item in self.containers:
            if item["id"] == container_id or item.get("name") == container_id:
                return item
        raise ContainerNotFoundError(container_id)

    def start_container(self, container_id):
        self.lifecycle_calls.append(("start_container", container_id))
        self._set_container_running(container_id, True)

    def stop_container(self, container_id):
        self.lifecycle_calls.append(("stop_container", container_id))
        self._set_container_running(container_id, False)

    def restart_container(self, container_id):
        self.lifecycle_calls.append(("restart_container", container_id))

    def remove_container(self, container_id):
        self.lifecycle_calls.append(("remove_container", container_id))
        self.containers = [item for item in self.containers if item["id"] != container_id]

    def start_compose_project(self, project_name):
        self.lifecycle_calls.append(("start_compose_project", project_name))
        self._set_compose_running(project_name, True)

    def stop_compose_project(self, project_name):
        self.lifecycle_calls.append(("stop_compose_project", project_name))
        self._set_compose_running(project_name, False)

    def restart_compose_project(self, project_name):
        self.lifecycle_calls.append(("restart_compose_project", project_name))

    def remove_compose_project(self, project_name):
        self.lifecycle_calls.append(("remove_compose_project", project_name))
        self.compose_projects = tuple(
            item for item in self.compose_projects if item.name != project_name
        )
        self.containers = [
            item
            for item in self.containers
            if item.get("labels", {}).get("com.docker.compose.project")
            != project_name
        ]

    def _set_container_running(self, container_id, running):
        item = next(item for item in self.containers if item["id"] == container_id)
        item["running"] = running
        item["status"] = "running" if running else "exited"

    def _set_compose_running(self, project_name, running):
        from docker_manage_server.docker_runtime import ComposeProjectRecord

        status = "running(1)" if running else "exited(1)"
        self.compose_projects = tuple(
            ComposeProjectRecord(item.name, status, item.config_files)
            if item.name == project_name
            else item
            for item in self.compose_projects
        )
        for item in self.containers:
            if (
                item.get("labels", {}).get("com.docker.compose.project")
                == project_name
            ):
                item["running"] = running
                item["status"] = "running" if running else "exited"

    def get_container(self, container_id):
        return SimpleNamespace(id=container_id, attrs={})

    def load_image(self, image_tar, cwd):
        return SimpleNamespace(returncode=0, stdout=b"loaded\n", stderr=b"")

    def compose_up(self, cwd):
        return SimpleNamespace(returncode=0, stdout=b"started\n", stderr=b"")

    def compose_config(self, project_dir, compose_file, env_file):
        return SimpleNamespace(
            returncode=self.compose_config_returncode,
            stdout=b"",
            stderr=self.compose_config_stderr,
        )

    def create_terminal(self, container_id, command):
        from docker_manage_server.docker_runtime import ContainerNotRunningError

        raise ContainerNotRunningError(container_id)


@pytest.fixture
def web_context(tmp_path):
    runtime = WebFakeRuntime()
    store = TaskStore(tmp_path)
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=store,
        runtime=runtime,
    )
    return TestClient(app), store, runtime
