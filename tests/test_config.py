import tempfile
from pathlib import Path

from app.config import ENVIRONMENT_VARIABLES, Settings, load_settings


def test_defaults_when_environment_is_empty():
    settings = load_settings({})
    assert settings.anthropic_api_key is None
    assert settings.anthropic_model == "claude-opus-5-5"
    assert settings.temporal_address == "localhost:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_api_key is None
    assert settings.task_queue == "repo-scanner"
    assert settings.scan_root == Path(tempfile.gettempdir()) / "repo-scanner"
    assert settings.github_token is None
    assert settings.demo_repo_url is None


def test_every_variable_is_read_from_environment():
    environ = {
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_MODEL": "claude-sonnet-5-5",
        "TEMPORAL_ADDRESS": "example.tmprl.cloud:7233",
        "TEMPORAL_NAMESPACE": "scanner.abc12",
        "TEMPORAL_API_KEY": "temporal-key",
        "TASK_QUEUE": "custom-queue",
        "SCAN_ROOT": "/var/tmp/scans",
        "GITHUB_TOKEN": "ghp_test",
        "DEMO_REPO_URL": "https://github.com/example/demo",
    }
    settings = load_settings(environ)
    assert settings.anthropic_api_key == "sk-test"
    assert settings.anthropic_model == "claude-sonnet-5-5"
    assert settings.temporal_address == "example.tmprl.cloud:7233"
    assert settings.temporal_namespace == "scanner.abc12"
    assert settings.temporal_api_key == "temporal-key"
    assert settings.task_queue == "custom-queue"
    assert settings.scan_root == Path("/var/tmp/scans")
    assert settings.github_token == "ghp_test"
    assert settings.demo_repo_url == "https://github.com/example/demo"


def test_blank_values_fall_back_to_defaults():
    settings = load_settings({"TEMPORAL_ADDRESS": "  ", "ANTHROPIC_API_KEY": ""})
    assert settings.temporal_address == "localhost:7233"
    assert settings.anthropic_api_key is None


def test_mapping_covers_every_setting():
    assert set(ENVIRONMENT_VARIABLES) == set(Settings.model_fields)
