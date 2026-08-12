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


def test_image_list_exposes_current_page_batch_delete_controls(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(index) for index in range(25)]

    response = client.get("/images?q=demo&page=2")

    assert response.status_code == 200
    assert "data-image-batch" in response.text
    assert 'data-preview-url="/api/images/batch-delete-preview"' in response.text
    assert 'data-delete-url="/api/images/batch-delete"' in response.text
    assert 'data-query="demo"' in response.text
    assert 'data-page="2"' in response.text
    assert "data-image-batch-enter" in response.text
    assert "data-image-batch-cancel" in response.text
    assert "data-image-batch-submit" in response.text
    assert "data-image-select-all" in response.text
    assert response.text.count("data-image-select-item") == 5
    assert 'value="sha256:image-4"' in response.text
    assert 'id="image-batch-delete-dialog"' in response.text
    assert "data-image-batch-confirm" in response.text
    assert 'type="module"' in response.text
    assert "/static/js/image_batch_delete.mjs" in response.text


def test_empty_image_list_disables_batch_entry(web_context):
    client, _store, runtime = web_context
    runtime.images = []

    response = client.get("/images")

    assert response.status_code == 200
    assert "data-image-batch-enter disabled" in response.text
    assert "data-image-select-item" not in response.text


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


def test_image_detail_embeds_delete_preview_dialog(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1", "demo/app:2"))]
    runtime.containers = [direct_reference()]

    response = client.get("/images/sha256:image-1")

    assert response.status_code == 200
    assert "镜像名称" in response.text
    assert 'data-dialog-open="image-delete-dialog"' in response.text
    assert 'id="image-delete-dialog"' in response.text
    assert "data-image-delete-dialog" in response.text
    assert (
        'data-delete-url="/api/images/sha256:image-1/tags"'
        in response.text
    )
    assert 'data-detail-url="/api/images/sha256:image-1"' in response.text
    assert 'action="/images/sha256:image-1/delete"' in response.text
    assert "demo/app:2" in response.text
    assert "demo/app:1" in response.text
    assert "极短的外部并发窗口" in response.text
    assert 'href="/images/sha256:image-1/delete"' not in response.text


def test_image_detail_exposes_no_script_delete_fallback(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1", "demo/app:2"))]
    runtime.containers = [direct_reference()]

    response = client.get("/images/sha256:image-1")

    assert response.status_code == 200
    assert "<noscript>" in response.text
    assert "JavaScript 不可用" in response.text
    assert "demo/app:2" in response.text
    assert 'data-image-delete-fallback' in response.text
    assert 'action="/images/sha256:image-1/delete"' in response.text


def test_image_delete_web_fallback_redirects_without_result_page(web_context):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1", "demo/app:2"))]
    runtime.containers = [direct_reference()]

    deleted = client.post(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/images/sha256:image-1"
    preview = client.get(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )
    assert preview.status_code == 307
    assert preview.headers["location"] == "/images/sha256:image-1"


def test_image_delete_web_fallback_returns_to_list_when_image_is_gone(
    web_context,
):
    client, _store, runtime = web_context
    runtime.images = [image_fixture(1, tags=("demo/app:1",))]
    runtime.containers = []

    response = client.post(
        "/images/sha256:image-1/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/images"


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
