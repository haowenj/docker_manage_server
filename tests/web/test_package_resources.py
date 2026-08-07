from importlib.resources import files
import json


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
