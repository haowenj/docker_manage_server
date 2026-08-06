from types import SimpleNamespace

from docker_manage_server.docker_runtime import DockerRuntime


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
