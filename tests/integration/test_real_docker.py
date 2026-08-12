import shutil
import subprocess

import pytest


docker_available = bool(
    shutil.which("docker")
    and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_health_and_container_listing(tmp_path):
    from starlette.testclient import TestClient

    from docker_manage_server.api import create_app
    from docker_manage_server.config import Settings

    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/api/health").status_code == 200
        assert isinstance(client.get("/api/containers").json()["items"], list)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_runtime_inventory_classifies_every_container():
    from docker_manage_server.docker_runtime import DockerRuntime
    from docker_manage_server.runtime_inventory import RuntimeInventoryService

    runtime = DockerRuntime()
    raw = runtime.list_containers()
    overview = RuntimeInventoryService(runtime).load()

    assert overview.docker_error is None
    classified = [
        item["id"]
        for project in overview.compose_projects
        for item in project.containers
    ] + [item["id"] for item in overview.standalone_containers]
    assert sorted(classified) == sorted(item["id"] for item in raw)
    assert len(classified) == len(set(classified))


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_standalone_container_lifecycle():
    from uuid import uuid4

    from docker.errors import ImageNotFound, NotFound

    from docker_manage_server.docker_runtime import DockerRuntime

    runtime = DockerRuntime()
    image = "alpine:3.21"
    try:
        runtime.client.images.get(image)
    except ImageNotFound:
        pytest.skip(f"local test image unavailable: {image}")

    name = f"docker-manage-lifecycle-{uuid4().hex}"
    created = runtime.client.containers.create(
        image,
        ["sh", "-c", "while true; do sleep 1; done"],
        name=name,
        labels={"docker-manage.test": "runtime-lifecycle"},
    )
    try:
        runtime.start_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is True
        runtime.restart_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is True
        runtime.stop_container(created.id)
        assert runtime.get_serialized_container(created.id)["running"] is False
        runtime.remove_container(created.id)
        with pytest.raises(NotFound):
            runtime.client.containers.get(created.id)
    finally:
        try:
            runtime.client.containers.get(created.id).remove(force=True, v=True)
        except NotFound:
            pass


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_image_inventory_and_safe_deletion():
    from io import BytesIO
    from uuid import uuid4

    from docker.errors import ImageNotFound, NotFound

    from docker_manage_server.docker_runtime import DockerRuntime
    from docker_manage_server.image_inventory import (
        ImageInUseError,
        ImageInventoryService,
    )

    runtime = DockerRuntime()
    try:
        runtime.client.images.get("alpine:3.21")
    except ImageNotFound:
        pytest.skip("local test image unavailable: alpine:3.21")

    token = uuid4().hex
    tag = f"docker-manage-image-test:{token}"
    container_name = f"docker-manage-image-test-{token}"
    image = None
    created = None
    dockerfile = (
        "FROM alpine:3.21\n"
        f'LABEL docker-manage.test="image-management" test.token="{token}"\n'
        'CMD ["sh", "-c", "while true; do sleep 1; done"]\n'
    ).encode()
    archive = BytesIO()
    import tarfile

    with tarfile.open(fileobj=archive, mode="w") as bundle:
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(dockerfile)
        bundle.addfile(info, BytesIO(dockerfile))
    archive.seek(0)

    try:
        image, _logs = runtime.client.images.build(
            fileobj=archive,
            custom_context=True,
            tag=tag,
            rm=True,
            labels={
                "docker-manage.test": "image-management",
                "test.token": token,
            },
        )
        service = ImageInventoryService(runtime)
        assert any(item.id == image.id for item in service.list(token).items)

        created = runtime.client.containers.create(
            tag,
            name=container_name,
            labels={
                "docker-manage.test": "image-management",
                "test.token": token,
            },
        )
        with pytest.raises(ImageInUseError):
            service.remove_available_tags(image.id)
        created.start()
        with pytest.raises(ImageInUseError):
            service.remove_available_tags(image.id)
        created.remove(force=True, v=True)
        created = None

        deleted = service.remove_available_tags(image.id)
        assert deleted.id == image.id
        assert deleted.deleted_tags == (tag,)
        with pytest.raises(ImageNotFound):
            runtime.client.images.get(image.id)
        image = None
    finally:
        if created is not None:
            try:
                current = runtime.client.containers.get(created.id)
                labels = current.attrs.get("Config", {}).get("Labels", {})
                if labels.get("test.token") == token:
                    current.remove(force=True, v=True)
            except NotFound:
                pass
        if image is not None:
            try:
                current_image = runtime.client.images.get(image.id)
                labels = current_image.attrs.get("Config", {}).get("Labels", {})
                if labels.get("test.token") == token:
                    runtime.client.images.remove(
                        current_image.id,
                        force=True,
                        noprune=False,
                    )
            except ImageNotFound:
                pass
