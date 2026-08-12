def image_fixture(index, tags=None):
    image_id = f"sha256:image-{index}"
    return {
        "id": image_id,
        "short_id": f"image-{index}",
        "tags": list(tags if tags is not None else (f"demo/item:{index}",)),
        "digests": [],
        "created": f"2026-08-{(index % 28) + 1:02d}T00:00:00Z",
        "size": 1024 * (index + 1),
        "architecture": "amd64",
        "os": "linux",
        "entrypoint": ["/entrypoint"],
        "command": ["serve"],
        "raw_attrs": {
            "Id": image_id,
            "Config": {"Labels": {"unsafe": "<script>"}},
        },
    }


def direct_reference():
    return {
        "id": "direct-full-id",
        "short_id": "direct",
        "name": "direct",
        "image": "demo/app:1",
        "image_id": "sha256:image-1",
        "image_reference": "demo/app:1",
        "status": "exited",
        "running": False,
        "labels": {},
        "ports": {},
        "mounts": [],
        "networks": {},
    }


def compose_reference():
    return {
        "id": "compose-full-id",
        "short_id": "compose",
        "name": "mall-web",
        "image": "demo/app:1",
        "image_id": "sha256:image-1",
        "image_reference": "demo/app:1",
        "status": "running",
        "running": True,
        "labels": {
            "com.docker.compose.project": "mall",
            "com.docker.compose.service": "web",
        },
        "ports": {},
        "mounts": [],
        "networks": {},
    }


def test_image_navigation_list_search_and_pagination(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(index) for index in range(25)]

    response = client.get("/images?q=demo&page=2")

    assert response.status_code == 200
    assert 'class="active" href="/images"' in response.text
    assert "共 25 个镜像" in response.text
    assert "第 2 / 2 页" in response.text
    assert "q=demo&amp;page=1" in response.text
    assert response.text.count('class="image-row"') == 5
    assert "引用容器" in response.text
    assert "镜像名称" in response.text
    assert "<th>Tags</th>" not in response.text
    assert 'href="/images/sha256:image-4"' in response.text
    assert ">demo/item:4</a>" in response.text


def test_untagged_image_uses_short_id_as_detail_link(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=())]

    response = client.get("/images")

    assert response.status_code == 200
    assert 'href="/images/sha256:image-1"' in response.text
    assert ">image-1</a>" in response.text
    assert "未标记" in response.text


def test_image_detail_renders_summary_escaped_inspect_and_links(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1",))]
    runtime.containers = [direct_reference(), compose_reference()]

    response = client.get("/images/sha256:image-1")

    assert response.status_code == 200
    assert "demo/app:1" in response.text
    assert "&lt;script&gt;" in response.text
    assert 'href="/containers/direct-full-id"' in response.text
    assert (
        'href="/compose-projects/mall?container=compose-full-id"'
        in response.text
    )
    assert "该镜像正被 2 个容器使用" in response.text
    assert 'action="/images/sha256:image-1/delete"' not in response.text


def test_unused_image_delete_previews_and_redirects_to_result(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1",))]
    runtime.containers = []

    detail = client.get("/images/sha256:image-1")
    assert (
        'href="/images/sha256:image-1/delete"'
        in detail.text
    )

    preview = client.get("/images/sha256:image-1/delete")
    assert "将删除的 Tags" in preview.text
    assert "demo/app:1" in preview.text
    assert "极短的外部并发窗口" in preview.text
    assert "data-confirm" in preview.text

    deleted = client.post(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"].startswith(
        "/images/tag-removal-results/"
    )
    result = client.get(deleted.headers["location"])
    assert "实际删除的 Tags" in result.text
    assert "demo/app:1" in result.text


def test_images_render_dangling_and_map_errors(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=())]
    assert "未标记" in client.get("/images").text
    assert client.get("/images?page=0").status_code == 422
    assert client.get("/images/missing").status_code == 404

    runtime.containers = [direct_reference()]
    assert client.post("/images/sha256:image-1/delete").status_code == 409
    runtime.image_error = "offline"
    assert client.get("/images").status_code == 503


def test_image_detail_maps_preview_container_failure_to_503(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1",))]
    original_list_containers = runtime.list_containers
    calls = 0

    def fail_second_container_list():
        nonlocal calls
        calls += 1
        if calls == 2:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError("container inspect failed")
        return original_list_containers()

    runtime.list_containers = fail_second_container_list

    response = client.get("/images/sha256:image-1")

    assert response.status_code == 503
    assert "container inspect failed" in response.text
