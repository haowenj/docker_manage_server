def test_unused_ports_page_renders_compact_grid_and_sidebar_entry(web_context):
    client, _store, runtime = web_context
    runtime.containers = [
        {
            **runtime.containers[0],
            "ports": {
                "8000/tcp": [{"HostPort": "6000"}],
                "8080/tcp": [{"HostPort": "6308"}],
            },
        }
    ]

    response = client.get("/unused-ports")

    assert response.status_code == 200
    assert "未使用端口查询" in response.text
    assert 'href="/unused-ports"' in response.text
    assert 'data-port="6000" data-port-state="mapped"' in response.text
    assert 'data-port="6001" data-port-state="available"' in response.text
    assert "可用端口：3998 个" in response.text


def test_unused_ports_page_returns_503_when_docker_is_offline(web_context):
    client, _store, runtime = web_context
    runtime.available = False

    response = client.get("/unused-ports")

    assert response.status_code == 503
    assert "Docker daemon 不可用" in response.text
