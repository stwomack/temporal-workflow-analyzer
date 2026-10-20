import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str | None = None
    task_queue: str = "repo-scanner"
    scan_root: Path = Path(tempfile.gettempdir()) / "repo-scanner"
    github_token: str | None = None
    demo_repo_url: str | None = None
    agent_max_turns: int = 40
    agent_max_budget_usd: float = 5.0
    worker_max_concurrent_activities: int = 2
    worker_shutdown_grace_seconds: float = 30.0
    temporal_ui_url: str = "http://localhost:8233"
    worker_host_id: str | None = None


ENVIRONMENT_VARIABLES = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL",
    "temporal_address": "TEMPORAL_ADDRESS",
    "temporal_namespace": "TEMPORAL_NAMESPACE",
    "temporal_api_key": "TEMPORAL_API_KEY",
    "task_queue": "TASK_QUEUE",
    "scan_root": "SCAN_ROOT",
    "github_token": "GITHUB_TOKEN",
    "demo_repo_url": "DEMO_REPO_URL",
    "agent_max_turns": "AGENT_MAX_TURNS",
    "agent_max_budget_usd": "AGENT_MAX_BUDGET_USD",
    "worker_max_concurrent_activities": "WORKER_MAX_CONCURRENT_ACTIVITIES",
    "worker_shutdown_grace_seconds": "WORKER_SHUTDOWN_GRACE_SECONDS",
    "temporal_ui_url": "TEMPORAL_UI_URL",
    "worker_host_id": "WORKER_HOST_ID",
}


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ
    values = {
        field: source[variable]
        for field, variable in ENVIRONMENT_VARIABLES.items()
        if source.get(variable, "").strip()
    }
    return Settings(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
