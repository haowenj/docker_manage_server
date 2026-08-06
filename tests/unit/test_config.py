from pathlib import Path

from docker_manage_server.config import Settings


def test_settings_defaults_to_app_data(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    settings = Settings()
    assert settings.data_dir == Path("/app/data")


def test_settings_reads_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "runtime"))
    assert Settings().data_dir == tmp_path / "runtime"
