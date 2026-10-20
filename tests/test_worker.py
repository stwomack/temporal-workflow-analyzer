import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app import temporal_client
from app.config import load_settings
from app.models import ScanRequest, Stage
from app.worker import ACTIVITIES, build_worker, remove_stale_scans
from app.workflows import RepoScanWorkflow

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_remove_stale_scans_keeps_recent_directories(tmp_path):
    now = time.time()
    old = tmp_path / "scan-old"
    recent = tmp_path / "scan-recent"
    old_file = tmp_path / "notes.txt"
    for directory in (old, recent):
        (directory / "repo").mkdir(parents=True)
    old_file.write_text("x")
    os.utime(old, (now - 3700, now - 3700))
    os.utime(old_file, (now - 3700, now - 3700))
    os.utime(recent, (now - 600, now - 600))

    assert remove_stale_scans(tmp_path, now=now) == ["scan-old"]
    assert not old.exists()
    assert recent.exists()
    assert old_file.exists()


def test_remove_stale_scans_handles_missing_root(tmp_path):
    assert remove_stale_scans(tmp_path / "missing") == []


@pytest.mark.parametrize(
    ("environ", "expected_tls"),
    [({}, False), ({"TEMPORAL_API_KEY": "cloud-key"}, True)],
)
async def test_connect_client_uses_api_key_with_tls(monkeypatch, environ, expected_tls):
    captured = {}

    async def fake_connect(target_host, **kwargs):
        captured["target_host"] = target_host
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(temporal_client.Client, "connect", fake_connect)
    settings = load_settings({"TEMPORAL_ADDRESS": "ns.acct.tmprl.cloud:7233", "TEMPORAL_NAMESPACE": "ns.acct", **environ})
    assert await temporal_client.connect_client(settings) == "client"
    assert captured["target_host"] == "ns.acct.tmprl.cloud:7233"
    assert captured["namespace"] == "ns.acct"
    assert captured["api_key"] == environ.get("TEMPORAL_API_KEY")
    assert captured["tls"] is expected_tls
    assert captured["data_converter"] is pydantic_data_converter


@pytest_asyncio.fixture
async def env():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_existing_path=shutil.which("temporal"),
    ) as environment:
        yield environment


async def test_build_worker_registers_everything_with_limits(env):
    settings = load_settings({"TASK_QUEUE": "custom-queue", "WORKER_MAX_CONCURRENT_ACTIVITIES": "3",
                              "WORKER_SHUTDOWN_GRACE_SECONDS": "12"})
    worker = build_worker(env.client, settings)
    config = worker.config()
    assert config["task_queue"] == "custom-queue"
    assert config["workflows"] == [RepoScanWorkflow]
    assert config["activities"] == ACTIVITIES
    assert {activity.__name__ for activity in ACTIVITIES} == {
        "clone_repo", "prepare_workspace", "run_agent_scan", "cleanup_workspace"
    }
    assert config["max_concurrent_activities"] == 3
    assert config["graceful_shutdown_timeout"].total_seconds() == 12
    assert config["max_heartbeat_throttle_interval"].total_seconds() == 5


def test_default_concurrency_is_two():
    assert load_settings({}).worker_max_concurrent_activities == 2


async def wait_for_line(process: subprocess.Popen, text: str, timeout: float) -> str:
    output = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = await asyncio.to_thread(process.stdout.readline)
        if not line:
            break
        output.append(line)
        if text in line:
            return "".join(output)
    raise AssertionError(f"Did not see {text!r} in worker output:\n{''.join(output)}")


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
async def test_worker_process_runs_scans_and_shuts_down_gracefully(env, tmp_path, stop_signal):
    scan_root = tmp_path / "scans"
    stale = scan_root / "scan-stale"
    stale.mkdir(parents=True)
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    task_queue = f"worker-test-{uuid.uuid4().hex[:8]}"
    environ = {
        **os.environ,
        "TEMPORAL_ADDRESS": env.client.service_client.config.target_host,
        "TEMPORAL_NAMESPACE": "default",
        "TASK_QUEUE": task_queue,
        "SCAN_ROOT": str(scan_root),
        "WORKER_SHUTDOWN_GRACE_SECONDS": "5",
        "PYTHONUNBUFFERED": "1",
    }
    environ.pop("TEMPORAL_API_KEY", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=PROJECT_ROOT,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        startup = await wait_for_line(process, "Worker polling task queue", timeout=30)
        assert "Removed 1 stale scan directories" in startup
        assert not stale.exists()

        handle = await env.client.start_workflow(
            RepoScanWorkflow.run,
            ScanRequest(repo_url="https://gitlab.com/example/demo"),
            id=f"scan-example-demo-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
        progress = await handle.query(RepoScanWorkflow.get_progress)
        assert progress.stage is Stage.failed
        assert "github.com" in progress.message

        process.send_signal(stop_signal)
        remaining = await asyncio.to_thread(process.communicate, timeout=20)
        assert process.returncode == 0, remaining[0]
        assert "Worker stopped" in remaining[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
