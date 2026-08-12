import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from docker.errors import DockerException

from docker_manage_server.docker_runtime import (
    ComposeListError,
    ComposeProjectRecord,
    DockerRuntime,
    DockerRuntimeError,
)


def test_list_containers_returns_ps_fields_and_raw_attrs():
    raw = {"Id": "abc", "Name": "/demo", "State": {"Running": True}}
    fake = SimpleNamespace(
        id="abc",
        short_id="abc",
        name="demo",
        image=SimpleNamespace(tags=["demo:latest"]),
        attrs=raw,
        status="Up 2 minutes",
        ports={"80/tcp": [{"HostPort": "8080"}]},
        labels={"app": "demo"},
    )
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda all=True: [fake]))
    result = DockerRuntime(client=client).list_containers()
    assert result[0]["id"] == "abc"
    assert result[0]["running"] is True
    assert result[0]["raw_attrs"] == raw


def test_list_containers_maps_docker_sdk_failure():
    def fail(**_kwargs):
        raise DockerException("daemon offline")

    client = SimpleNamespace(containers=SimpleNamespace(list=fail))
    with pytest.raises(DockerRuntimeError, match="daemon offline"):
        DockerRuntime(client=client).list_containers()


def test_compose_up_uses_fixed_directory_and_no_shell(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    result = DockerRuntime(client=SimpleNamespace(), command_runner=runner).compose_up(tmp_path)
    assert result.returncode == 0
    assert calls[0][0] == ["docker", "compose", "--project-directory", str(tmp_path), "up", "-d"]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["shell"] is False


def test_compose_config_uses_candidate_files_project_directory_and_no_shell(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    compose_file = tmp_path / ".compose.candidate.yaml"
    env_file = tmp_path / ".env.candidate"
    result = DockerRuntime(
        client=SimpleNamespace(), command_runner=runner
    ).compose_config(tmp_path, compose_file, env_file)

    assert result.returncode == 0
    assert calls[0][0] == [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
        "config",
        "--quiet",
    ]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["shell"] is False


def test_list_compose_projects_uses_all_json_and_no_shell():
    calls = []
    payload = [
        {
            "Name": "mall-stack",
            "Status": "running(3)",
            "ConfigFiles": "/srv/mall/compose.yaml,/srv/mall/compose.prod.yaml",
        }
    ]

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)
    assert runtime.list_compose_projects() == (
        ComposeProjectRecord(
            name="mall-stack",
            status="running(3)",
            config_files=(
                "/srv/mall/compose.yaml",
                "/srv/mall/compose.prod.yaml",
            ),
        ),
    )
    assert calls[0][0] == ["docker", "compose", "ls", "--all", "--format", "json"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 1800


def test_list_compose_projects_accepts_empty_json_array():
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=b"[]\n", stderr=b""
        ),
    )
    assert runtime.list_compose_projects() == ()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"compose unavailable"
            ),
            "compose unavailable",
        ),
        (
            SimpleNamespace(returncode=0, stdout=b"not-json", stderr=b""),
            "invalid docker compose ls JSON",
        ),
        (
            SimpleNamespace(returncode=0, stdout=b"{}", stderr=b""),
            "expected a list",
        ),
    ],
)
def test_list_compose_projects_maps_failures(result, message):
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: result,
    )
    with pytest.raises(ComposeListError, match=message):
        runtime.list_compose_projects()


def test_list_compose_projects_maps_command_execution_failure():
    def runner(*_args, **_kwargs):
        raise OSError("docker not found")

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)
    with pytest.raises(ComposeListError, match="docker not found"):
        runtime.list_compose_projects()


def test_logs_passes_tail_and_timestamps():
    calls = {}

    def logs(**kwargs):
        calls.update(kwargs)
        return b"hello\n"

    container = SimpleNamespace(
        logs=logs
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: container))
    runtime = DockerRuntime(client=client)
    output = runtime.logs("abc", tail="100", timestamps=True)
    assert output == b"hello\n"
    assert calls == {"tail": "100", "timestamps": True}


@pytest.mark.parametrize(
    ("method_name", "container_method", "expected_kwargs"),
    [
        ("start_container", "start", {}),
        ("stop_container", "stop", {}),
        ("restart_container", "restart", {}),
        ("remove_container", "remove", {"force": False, "v": False}),
    ],
)
def test_container_lifecycle_uses_docker_sdk(
    method_name, container_method, expected_kwargs
):
    calls = []
    container = SimpleNamespace(
        **{container_method: lambda **kwargs: calls.append(kwargs)}
    )
    runtime = DockerRuntime(
        client=SimpleNamespace(
            containers=SimpleNamespace(get=lambda _container_id: container)
        )
    )

    getattr(runtime, method_name)("immutable-id")

    assert calls == [expected_kwargs]


def test_container_lifecycle_maps_sdk_failure():
    def fail():
        raise DockerException("operation failed")

    runtime = DockerRuntime(
        client=SimpleNamespace(
            containers=SimpleNamespace(
                get=lambda _container_id: SimpleNamespace(start=fail)
            )
        )
    )

    with pytest.raises(DockerRuntimeError, match="operation failed"):
        runtime.start_container("immutable-id")


@pytest.mark.parametrize(
    ("method_name", "subcommand"),
    [
        ("start_compose_project", "start"),
        ("stop_compose_project", "stop"),
        ("restart_compose_project", "restart"),
        ("remove_compose_project", "down"),
    ],
)
def test_compose_lifecycle_uses_project_name_empty_directory_and_no_shell(
    method_name, subcommand
):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    runtime = DockerRuntime(client=SimpleNamespace(), command_runner=runner)

    getattr(runtime, method_name)("mall")

    argv, kwargs = calls[0]
    assert argv == ["docker", "compose", "--project-name", "mall", subcommand]
    assert kwargs["shell"] is False
    assert Path(kwargs["cwd"]).name.startswith("docker-manage-compose-")
    assert not Path(kwargs["cwd"]).exists()


def test_compose_lifecycle_maps_nonzero_exit():
    runtime = DockerRuntime(
        client=SimpleNamespace(),
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"compose failed"
        ),
    )

    with pytest.raises(DockerRuntimeError, match="compose failed"):
        runtime.stop_compose_project("mall")
