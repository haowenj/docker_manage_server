from importlib.resources import files
import json
from pathlib import Path
import tomllib


def test_package_data_covers_every_template_directory():
    project_root = Path(__file__).resolve().parents[2]
    package_root = project_root / "src/docker_manage_server"
    config = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    patterns = set(
        config["tool"]["setuptools"]["package-data"]["docker_manage_server"]
    )
    template_directories = {
        path.parent.relative_to(package_root).as_posix()
        for path in package_root.glob("templates/**/*.html")
    }

    assert {
        f"{directory}/*.html" for directory in template_directories
    } <= patterns


def test_vendored_terminal_resources_are_present_and_pinned():
    vendor = files("docker_manage_server").joinpath("static/vendor/xterm")
    for name in (
        "xterm.mjs",
        "addon-fit.mjs",
        "xterm.css",
        "LICENSE-xterm.txt",
        "LICENSE-addon-fit.txt",
    ):
        assert vendor.joinpath(name).is_file()
    versions = json.loads(vendor.joinpath("versions.json").read_text(encoding="utf-8"))
    assert versions == {
        "@xterm/xterm": "6.0.0",
        "@xterm/addon-fit": "0.11.0",
    }


def test_deployment_editor_assets_are_packaged():
    package = files("docker_manage_server")
    assert package.joinpath("templates/deployments/edit.html").is_file()
    script = package.joinpath("static/js/app.js").read_text(encoding="utf-8")
    assert "data-directory-editor" in script


def test_compose_dialog_script_uses_id_without_css_escape():
    script = (
        files("docker_manage_server")
        .joinpath("static/js/app.js")
        .read_text(encoding="utf-8")
    )
    assert "document.getElementById(button.dataset.dialogOpen)" in script
    assert "document.getElementById(autoOpenDialogId)" in script
    assert "CSS.escape" not in script


def test_image_delete_dialog_uses_same_origin_api_and_safe_dom_updates():
    script = (
        files("docker_manage_server")
        .joinpath("static/js/app.js")
        .read_text(encoding="utf-8")
    )
    assert 'document.querySelectorAll("[data-image-delete-dialog]")' in script
    assert 'method: "DELETE"' in script
    assert "dialog.dataset.deleteUrl" in script
    assert "dialog.dataset.detailUrl" in script
    assert "response.ok" in script
    assert "replaceChildren" in script
    assert "textContent" in script
    assert "innerHTML" not in script


def test_image_batch_delete_module_is_packaged():
    module = (
        files("docker_manage_server")
        .joinpath("static/js/image_batch_delete.mjs")
    )
    assert module.is_file()


def test_image_batch_delete_module_uses_safe_dom_updates():
    script = (
        files("docker_manage_server")
        .joinpath("static/js/image_batch_delete.mjs")
        .read_text(encoding="utf-8")
    )
    assert "data-image-batch" in script
    assert 'method: "POST"' in script
    assert '"Content-Type": "application/json"' in script
    assert "replaceChildren" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "window.location.assign" in script


def test_hidden_state_overrides_component_display_styles():
    stylesheet = (
        files("docker_manage_server")
        .joinpath("static/css/app.css")
        .read_text(encoding="utf-8")
    )
    assert "[hidden] { display: none !important; }" in stylesheet


def test_runtime_templates_are_packaged():
    package = files("docker_manage_server")
    for path in (
        "templates/runtime/list.html",
        "templates/compose_projects/detail.html",
        "templates/compose_projects/logs.html",
        "templates/compose_projects/terminal.html",
        "templates/images/list.html",
        "templates/images/detail.html",
        "templates/images/delete.html",
        "templates/images/delete_result.html",
    ):
        assert package.joinpath(path).is_file()
