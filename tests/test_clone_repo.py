import dataclasses
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import activities
from app.activities import CLONE_MARKER_NAME, CloneRequest, CloneResult, classify_git_failure, clone_repo
from app.config import get_settings

REPO_URL = "https://github.com/example/demo"
CLONE_START_TO_CLOSE_SECONDS = 180


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "sources"
    source = root / "example" / "demo.git"
    source.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    (source / "workflow.py").write_text("print('hello')\n")
    (source / "pkg").mkdir()
    (source / "pkg" / "activities.py").write_text("x = 1\n")
    os.symlink("/etc/passwd", source / "secret-link")
    git("add", "-A", cwd=source)
    git("commit", "-q", "-m", "initial", cwd=source)
    return root


@pytest.fixture
def scan_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scans"
    monkeypatch.setenv("SCAN_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def redirect_github(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{base}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")


@pytest.fixture
def local_github(source_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    redirect_github(monkeypatch, f"file://{source_root}/")
    return source_root


def environment(workflow_id: str | None = None) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=workflow_id or f"scan-example-demo-{uuid.uuid4().hex[:8]}")
    return env


async def run_clone(env: ActivityEnvironment, url: str = REPO_URL) -> CloneResult:
    return await env.run(clone_repo, CloneRequest(repo_url=url))


async def expect_error(env: ActivityEnvironment, url: str = REPO_URL) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await run_clone(env, url)
    return caught.value


async def test_clone_returns_small_result(scan_root, local_github):
    env = environment("scan-example-demo-aaaa1111")
    result = await run_clone(env)
    repo = scan_root / "scan-example-demo-aaaa1111" / "repo"
    assert result.clone_path == str(repo)
    assert result.file_count == 3
    assert result.size_bytes > 0
    assert set(dataclasses.asdict(result)) == {"clone_path", "file_count", "size_bytes"}
    assert (repo / "pkg" / "activities.py").read_text() == "x = 1\n"
    assert subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip() == "1"


async def test_symlinks_are_not_materialized(scan_root, local_github):
    result = await run_clone(environment())
    link = Path(result.clone_path) / "secret-link"
    assert not link.is_symlink()
    assert link.read_text() == "/etc/passwd"


async def test_completed_clone_is_reused_without_recloning(scan_root, local_github, monkeypatch):
    env = environment()
    first = await run_clone(env)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("git clone should not run again")

    monkeypatch.setattr(activities, "run_git_clone", fail_if_called)
    second = await run_clone(env)
    assert second == first


async def test_partial_directory_is_removed_before_cloning(scan_root, local_github):
    workflow_id = "scan-example-demo-partial1"
    partial = scan_root / workflow_id / "repo"
    partial.mkdir(parents=True)
    (partial / "leftover.txt").write_text("half written")
    result = await run_clone(environment(workflow_id))
    assert not (Path(result.clone_path) / "leftover.txt").exists()
    assert (Path(result.clone_path) / "workflow.py").exists()


async def test_marker_for_different_repo_forces_reclone(scan_root, local_github):
    workflow_id = "scan-example-demo-other123"
    workspace = scan_root / workflow_id
    (workspace / "repo").mkdir(parents=True)
    (workspace / CLONE_MARKER_NAME).write_text(
        json.dumps({"clone_url": "https://github.com/someone/else.git", "clone_path": str(workspace / "repo"),
                    "file_count": 0, "size_bytes": 0})
    )
    result = await run_clone(environment(workflow_id))
    assert result.file_count == 3


async def test_too_many_files_is_non_retryable(scan_root, local_github, monkeypatch):
    monkeypatch.setattr(activities, "MAX_REPO_FILES", 2)
    env = environment()
    error = await expect_error(env)
    assert error.type == "RepoTooLarge"
    assert error.non_retryable
    assert not (scan_root / env.info.workflow_id / "repo").exists()


async def test_too_many_bytes_is_non_retryable(scan_root, local_github, monkeypatch):
    monkeypatch.setattr(activities, "MAX_REPO_BYTES", 10)
    error = await expect_error(environment())
    assert error.type == "RepoTooLarge"
    assert error.non_retryable


def test_default_limits_match_requirements():
    assert activities.MAX_REPO_BYTES == 200 * 1024 * 1024
    assert activities.MAX_REPO_FILES == 20_000


def test_git_timeout_is_shorter_than_activity_timeout():
    assert activities.GIT_CLONE_TIMEOUT_SECONDS < CLONE_START_TO_CLOSE_SECONDS


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "https://github.com/a/--upload-pack=x", "not a url"])
async def test_invalid_url_is_non_retryable(scan_root, url):
    error = await expect_error(environment(), url)
    assert error.type == "InvalidRepoUrl"
    assert error.non_retryable


async def test_unsafe_workflow_id_is_non_retryable(scan_root, local_github):
    error = await expect_error(environment("../escape"))
    assert error.type == "InvalidWorkflowId"
    assert error.non_retryable


async def test_missing_repository_is_non_retryable(scan_root, local_github):
    error = await expect_error(environment(), "https://github.com/example/missing")
    assert error.type == "RepoNotFound"
    assert error.non_retryable


async def test_network_failure_is_retryable(scan_root, monkeypatch):
    redirect_github(monkeypatch, "http://127.0.0.1:9/")
    error = await expect_error(environment())
    assert error.type == "CloneNetworkError"
    assert not error.non_retryable


async def test_git_timeout_is_retryable_and_kills_process(scan_root, monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    monkeypatch.setattr(activities, "GIT_CLONE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        activities, "git_clone_command",
        lambda clone_url, destination: ["sh", "-c", f"echo $$ > {pid_file}; exec sleep 30"],
    )
    error = await expect_error(environment())
    assert error.type == "CloneTimeout"
    assert not error.non_retryable
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    ("stderr", "expected_type", "non_retryable"),
    [
        ("remote: Repository not found.\nfatal: repository 'https://github.com/a/b.git/' not found", "RepoNotFound", True),
        ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", "RepoNotFound", True),
        ("fatal: unable to access 'https://github.com/a/b.git/': Could not resolve host: github.com", "CloneNetworkError", False),
        ("fatal: unable to access 'https://github.com/a/b.git/': Failed to connect to github.com port 443", "CloneNetworkError", False),
        ("error: RPC failed; curl 92\nfatal: early EOF", "CloneNetworkError", False),
        ("fatal: something unexpected", "CloneFailed", False),
        ("", "CloneFailed", False),
    ],
)
def test_git_failure_classification(stderr, expected_type, non_retryable):
    error = classify_git_failure(stderr)
    assert error.type == expected_type
    assert error.non_retryable is non_retryable
