"""The data backend ships the config imported by its shared data services."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_data_backend_image_includes_dashboard_config():
    dockerfile = (ROOT/'services/data_backend/Dockerfile').read_text()
    assert 'COPY dashboard/backend/config.yaml ./dashboard/backend/config.yaml' in dockerfile


def _load_config_module():
    spec = importlib.util.spec_from_file_location(
        'dashboard_config_packaging_test', ROOT/'dashboard/backend/core/config.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_config_preserves_default_settings_and_port_type(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ('DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    module = _load_config_module()
    defaults = module.Settings(_env_file=None)
    assert module.settings.model_dump() == defaults.model_dump()
    assert type(module.settings.db_port) is int


def test_packaged_config_preserves_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_HOST', 'configured-postgres')
    monkeypatch.setenv('DB_PORT', '6543')
    monkeypatch.setenv('DB_NAME', 'configured_database')
    monkeypatch.setenv('DB_USER', 'configured_runtime')
    module = _load_config_module()
    assert module.settings.db_host == 'configured-postgres'
    assert module.settings.db_port == 6543
    assert type(module.settings.db_port) is int
    assert module.settings.db_name == 'configured_database'
    assert module.settings.db_user == 'configured_runtime'
