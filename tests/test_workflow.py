import asyncio
import shutil
from contextlib import asynccontextmanager
import uuid
from collections import Counter
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.api.enums.v1 import TimeoutType
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from app.activities import AgentScanRequest, CloneRequest, CloneResult, WorkspaceResult
from app.models import PROGRESS_SIGNAL_NAME, ScanProgress, ScanReport, ScanRequest, Stage
from app import workflows as workflows_module
from app.workflows import RepoScanWorkflow

pytestmark = pytest.mark.asyncio(loop_scope="module")

REPO_URL = "https://github.com/example/demo"
REPORT = {
    "repo_url": REPO_URL,
    "detected_languages": ["python"],
    "summary": "One problem.",
    "findings": [
        {
            "rank": 1,
            "severity": "high",
            "category": "timeouts",
            "title": "No timeout",
            "file": "app.py",
            "line": 3,
            "problem": "An activity has no timeout.",
            "fix": "Add start_to_close_timeout.",
            "doc_url": None,
        }
    ],
    "truncated": False,
}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    existing = shutil.which("temporal")
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_existing_path=existing,
    ) as environment:
        yield environment


class MockActivities:
    def __init__(self, clone_error=None, agent_error=None, agent_blocks=False):
        self.calls = Counter()
        self.clone_error = clone_error
        self.agent_error = agent_error
        self.agent_blocks = agent_blocks
        self.agent_started = asyncio.Event()
        self.release_agent = asyncio.Event()
        self.agent_cancelled = False

    def activities(self):
        @activity.defn(name="clone_repo")
        async def clone_repo(request: CloneRequest) -> CloneResult:
            self.calls["clone_repo"] += 1
            if self.clone_error:
                raise self.clone_error
            return CloneResult(clone_path="/tmp/x/repo", file_count=3, size_bytes=100)

        @activity.defn(name="prepare_workspace")
        async def prepare_workspace() -> WorkspaceResult:
            self.calls["prepare_workspace"] += 1
            return WorkspaceResult(workspace_path="/tmp/x", skills=["temporal-developer"])

        @activity.defn(name="run_agent_scan")
        async def run_agent_scan(request: AgentScanRequest) -> dict:
            self.calls["run_agent_scan"] += 1
            if self.agent_error:
                raise self.agent_error
            self.agent_started.set()
            if self.agent_blocks:
                try:
                    while not self.release_agent.is_set():
                        activity.heartbeat("scanning")
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    self.agent_cancelled = True
                    raise
            return dict(REPORT)

        @activity.defn(name="cleanup_workspace")
        async def cleanup_workspace() -> bool:
            self.calls["cleanup_workspace"] += 1
            return True

        return [clone_repo, prepare_workspace, run_agent_scan, cleanup_workspace]


@asynccontextmanager
async def worker(
    client: Client, mocks: MockActivities, task_queue: str, dead_queues: list[str] | None = None, sandboxed: bool = True
):
    host_queue = f"{task_queue}-host"
    pending_dead = list(dead_queues or [])

    @activity.defn(name="get_worker_task_queue")
    async def get_worker_task_queue() -> str:
        mocks.calls["get_worker_task_queue"] += 1
        return pending_dead.pop(0) if pending_dead else host_queue

    runner = {} if sandboxed else {"workflow_runner": UnsandboxedWorkflowRunner()}
    async with Worker(
        client, task_queue=task_queue, workflows=[RepoScanWorkflow], activities=[get_worker_task_queue], **runner
    ), Worker(client, task_queue=host_queue, activities=mocks.activities()):
        yield host_queue


async def start(client: Client, task_queue: str):
    return await client.start_workflow(
        RepoScanWorkflow.run,
        ScanRequest(repo_url=REPO_URL),
        id=f"scan-example-demo-{uuid.uuid4().hex[:8]}",
        task_queue=task_queue,
    )


async def progress(handle) -> ScanProgress:
    return await handle.query(RepoScanWorkflow.get_progress)


async def test_success_runs_activities_in_order_and_cleans_up(env):
    mocks = MockActivities()
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        result = await handle.result()
        state = await progress(handle)

    assert ScanReport.model_validate(result).findings[0].title == "No timeout"
    assert isinstance(result, dict)
    assert mocks.calls == Counter(
        get_worker_task_queue=1, clone_repo=1, prepare_workspace=1, run_agent_scan=1, cleanup_workspace=1
    )
    assert state.stage is Stage.done

    history = await handle.fetch_history()
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == ["get_worker_task_queue", "clone_repo", "prepare_workspace", "run_agent_scan", "cleanup_workspace"]
    await Replayer(workflows=[RepoScanWorkflow], data_converter=pydantic_data_converter).replay_workflow(history)


async def test_activity_options_match_requirements(env):
    mocks = MockActivities()
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue) as host_queue:
        handle = await start(env.client, task_queue)
        await handle.result()
    history = await handle.fetch_history()
    options = {
        event.activity_task_scheduled_event_attributes.activity_type.name: event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    claim = options["get_worker_task_queue"]
    assert claim.task_queue.name == task_queue
    assert claim.start_to_close_timeout.ToSeconds() == 30
    for name in ["clone_repo", "prepare_workspace", "run_agent_scan", "cleanup_workspace"]:
        assert options[name].task_queue.name == host_queue
    for name in ["clone_repo", "prepare_workspace", "run_agent_scan"]:
        assert options[name].schedule_to_start_timeout.ToSeconds() == 600
    assert options["cleanup_workspace"].schedule_to_start_timeout.ToSeconds() == 60
    clone = options["clone_repo"]
    assert clone.start_to_close_timeout.ToSeconds() == 180
    assert clone.retry_policy.maximum_attempts == 3
    assert clone.retry_policy.initial_interval.ToSeconds() == 1
    assert clone.retry_policy.backoff_coefficient == 2.0
    assert list(clone.retry_policy.non_retryable_error_types) == ["InvalidRepoUrl", "RepoNotFound", "RepoTooLarge"]
    prepare = options["prepare_workspace"]
    assert prepare.start_to_close_timeout.ToSeconds() == 60
    assert prepare.retry_policy.maximum_attempts == 3
    agent = options["run_agent_scan"]
    assert agent.start_to_close_timeout.ToSeconds() == 900
    assert agent.heartbeat_timeout.ToSeconds() == 60
    assert agent.retry_policy.maximum_attempts == 2
    assert list(agent.retry_policy.non_retryable_error_types) == ["MissingApiKey"]
    cleanup = options["cleanup_workspace"]
    assert cleanup.start_to_close_timeout.ToSeconds() == 60
    assert cleanup.retry_policy.maximum_attempts == 3


@pytest.mark.parametrize("error_type", ["RepoNotFound", "InvalidRepoUrl", "RepoTooLarge"])
async def test_clone_failure_with_non_retryable_type_is_not_retried(env, error_type):
    mocks = MockActivities(clone_error=ApplicationError("clone failed", type=error_type))
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        with pytest.raises(WorkflowFailureError) as caught:
            await handle.result()
        state = await progress(handle)

    assert mocks.calls["clone_repo"] == 1
    assert mocks.calls["run_agent_scan"] == 0
    assert mocks.calls["cleanup_workspace"] == 1
    assert state.stage is Stage.failed
    assert state.message
    assert isinstance(caught.value.cause, ApplicationError)
    assert caught.value.cause.type == error_type


async def test_repo_not_found_message_is_human_readable(env):
    mocks = MockActivities(clone_error=ApplicationError("git exit 128", type="RepoNotFound"))
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        state = await progress(handle)
    assert state.message == "Repository not found or not public."


async def test_agent_failure_non_retryable_fails_scan(env):
    mocks = MockActivities(agent_error=ApplicationError("no key", type="MissingApiKey"))
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        state = await progress(handle)
    assert mocks.calls["run_agent_scan"] == 1
    assert mocks.calls["cleanup_workspace"] == 1
    assert state.stage is Stage.failed
    assert state.message == "The worker has no ANTHROPIC_API_KEY configured."


async def test_agent_failure_retryable_is_attempted_twice(env):
    mocks = MockActivities(agent_error=ApplicationError("bad output", type="InvalidAgentOutput"))
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        state = await progress(handle)
    assert mocks.calls["run_agent_scan"] == 2
    assert mocks.calls["cleanup_workspace"] == 1
    assert state.message == "The agent did not return a valid report."


async def test_cancellation_sets_stage_and_still_cleans_up(env):
    mocks = MockActivities(agent_blocks=True)
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        await asyncio.wait_for(mocks.agent_started.wait(), timeout=10)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as caught:
            await handle.result()
        state = await progress(handle)

    assert isinstance(caught.value.cause, CancelledError)
    assert mocks.agent_cancelled
    assert mocks.calls["cleanup_workspace"] == 1
    assert state.stage is Stage.cancelled
    history = await handle.fetch_history()
    names = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert names[-1] == "cleanup_workspace"
    await Replayer(workflows=[RepoScanWorkflow], data_converter=pydantic_data_converter).replay_workflow(history)


async def test_signals_update_progress_and_keep_latest_25(env):
    mocks = MockActivities(agent_blocks=True)
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        await asyncio.wait_for(mocks.agent_started.wait(), timeout=10)
        for index in range(30):
            await handle.signal(PROGRESS_SIGNAL_NAME, f"event {index}")
        state = await progress(handle)
        assert state.stage is Stage.scanning
        assert len(state.events) == 25
        assert state.events[0] == "event 5"
        assert state.events[-1] == "event 29"
        mocks.release_agent.set()
        await handle.result()
        final = await progress(handle)
    assert final.stage is Stage.done
    assert final.events[-1] == "event 29"


async def test_dead_worker_queue_moves_scan_to_another_worker(env, monkeypatch):
    monkeypatch.setattr(workflows_module, "HOST_SCHEDULE_TO_START", timedelta(seconds=2))
    mocks = MockActivities()
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue, dead_queues=[f"{task_queue}-dead"], sandboxed=False):
        handle = await start(env.client, task_queue)
        result = await asyncio.wait_for(handle.result(), timeout=30)
        state = await progress(handle)
    history = await handle.fetch_history()

    assert ScanReport.model_validate(result).findings
    assert mocks.calls["get_worker_task_queue"] == 2
    assert mocks.calls["clone_repo"] == 1
    assert "worker unavailable, restarting scan on another worker" in state.events
    timed_out = [
        event.activity_task_timed_out_event_attributes.failure.timeout_failure_info.timeout_type
        for event in history.events
        if event.HasField("activity_task_timed_out_event_attributes")
    ]
    assert timed_out == [TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_START]


async def test_non_retryable_failure_is_not_moved_to_another_worker(env):
    mocks = MockActivities(clone_error=ApplicationError("nope", type="RepoNotFound"))
    task_queue = str(uuid.uuid4())
    async with worker(env.client, mocks, task_queue):
        handle = await start(env.client, task_queue)
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert mocks.calls["get_worker_task_queue"] == 1
