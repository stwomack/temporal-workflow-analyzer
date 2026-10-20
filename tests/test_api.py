import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.activities import AgentScanRequest, CloneRequest, CloneResult, WorkspaceResult
from app.api import create_app
from app.config import load_settings
from app.models import ScanProgress, ScanRequest, Stage
from app.worker import MAX_HEARTBEAT_THROTTLE
from app.workflows import RepoScanWorkflow

REPORT = {
    "repo_url": "https://github.com/temporalio/samples-python",
    "detected_languages": ["python"],
    "summary": "One problem.",
    "findings": [
        {
            "rank": 1,
            "severity": "critical",
            "category": "non_determinism",
            "title": "Clock read in workflow",
            "file": "workflow.py",
            "line": 4,
            "problem": "datetime.now() in workflow code.",
            "fix": "Use workflow.now().",
            "doc_url": None,
        }
    ],
    "truncated": False,
}


@dataclass
class FakeDescription:
    status: WorkflowExecutionStatus


@dataclass
class FakeScan:
    status: WorkflowExecutionStatus = WorkflowExecutionStatus.RUNNING
    progress: ScanProgress | None = None
    result: dict | None = None
    error: BaseException | None = None
    cancelled: bool = False
    started: dict = field(default_factory=dict)


class FakeHandle:
    def __init__(self, scan: FakeScan | None):
        self.scan = scan

    async def describe(self):
        if self.scan is None:
            raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
        return FakeDescription(self.scan.status)

    async def query(self, query):
        assert query == RepoScanWorkflow.get_progress
        if self.scan.progress is None:
            raise RPCError("no poller", RPCStatusCode.FAILED_PRECONDITION, b"")
        return self.scan.progress

    async def result(self):
        if self.scan.error:
            raise self.scan.error
        return self.scan.result

    async def cancel(self):
        self.scan.cancelled = True


class FakeTemporalClient:
    def __init__(self):
        self.scans: dict[str, FakeScan] = {}

    async def start_workflow(self, run, request, *, id, task_queue):
        assert run == RepoScanWorkflow.run
        self.scans[id] = FakeScan(started={"request": request, "task_queue": task_queue})
        return FakeHandle(self.scans[id])

    def get_workflow_handle(self, workflow_id):
        return FakeHandle(self.scans.get(workflow_id))


@pytest.fixture
def fake():
    return FakeTemporalClient()


@pytest.fixture
def factory_calls():
    return []


@pytest.fixture
def api(fake, factory_calls):
    async def factory(settings):
        factory_calls.append(settings)
        return fake

    settings = load_settings({"TASK_QUEUE": "scanner-queue", "TEMPORAL_NAMESPACE": "demo"})
    with TestClient(create_app(factory, settings)) as client:
        yield client


def start_scan(api, url="https://github.com/temporalio/samples-python.git/") -> dict:
    response = api.post("/api/scans", json={"repo_url": url})
    assert response.status_code == 200, response.text
    return response.json()


def test_start_scan_starts_workflow_with_generated_id(api, fake):
    body = start_scan(api)
    scan_id = body["scan_id"]
    assert re.fullmatch(r"scan-temporalio-samples-python-[0-9a-f]{8}", scan_id)
    assert body["temporal_ui_url"] == f"http://localhost:8233/namespaces/demo/workflows/{scan_id}"
    started = fake.scans[scan_id].started
    assert started["task_queue"] == "scanner-queue"
    assert started["request"] == ScanRequest(repo_url="https://github.com/temporalio/samples-python")


def test_scan_ids_are_unique(api):
    assert start_scan(api)["scan_id"] != start_scan(api)["scan_id"]


@pytest.mark.parametrize(
    "url",
    ["https://gitlab.com/a/b", "https://github.com/a/b?x=1", "https://github.com/-rf/x", "not a url"],
)
def test_invalid_url_returns_422_with_message(api, fake, url):
    response = api.post("/api/scans", json={"repo_url": url})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert response.json()["detail"]
    assert fake.scans == {}


def test_missing_repo_url_returns_422(api):
    assert api.post("/api/scans", json={}).status_code == 422


def test_running_scan_returns_progress(api, fake):
    scan_id = start_scan(api)["scan_id"]
    fake.scans[scan_id].progress = ScanProgress(stage="scanning", message="Agent is reviewing", events=["file read a.py"])
    body = api.get(f"/api/scans/{scan_id}").json()
    assert body["stage"] == "scanning"
    assert body["message"] == "Agent is reviewing"
    assert body["events"] == ["file read a.py"]
    assert body["report"] is None
    assert body["error"] is None


def test_running_scan_without_worker_is_queued(api, fake):
    scan_id = start_scan(api)["scan_id"]
    body = api.get(f"/api/scans/{scan_id}").json()
    assert body["stage"] == "queued"


def test_completed_scan_returns_report_and_done(api, fake):
    scan_id = start_scan(api)["scan_id"]
    scan = fake.scans[scan_id]
    scan.status = WorkflowExecutionStatus.COMPLETED
    scan.result = REPORT
    body = api.get(f"/api/scans/{scan_id}").json()
    assert body["stage"] == "done"
    assert body["report"]["findings"][0]["title"] == "Clock read in workflow"
    assert body["error"] is None


def test_failed_scan_returns_error_message(api, fake):
    scan_id = start_scan(api)["scan_id"]
    scan = fake.scans[scan_id]
    scan.status = WorkflowExecutionStatus.FAILED
    scan.error = WorkflowFailureError(cause=ApplicationError("Repository not found or not public.", type="RepoNotFound"))
    body = api.get(f"/api/scans/{scan_id}").json()
    assert body["stage"] == "failed"
    assert body["error"] == "Repository not found or not public."


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (WorkflowExecutionStatus.CANCELED, "cancelled"),
        (WorkflowExecutionStatus.TERMINATED, "failed"),
        (WorkflowExecutionStatus.TIMED_OUT, "failed"),
    ],
)
def test_other_closed_states_return_error(api, fake, status, stage):
    scan_id = start_scan(api)["scan_id"]
    fake.scans[scan_id].status = status
    body = api.get(f"/api/scans/{scan_id}").json()
    assert body["stage"] == stage
    assert body["error"]


def test_unknown_scan_returns_404(api):
    assert api.get("/api/scans/scan-nope-nope-00000000").status_code == 404
    assert api.post("/api/scans/scan-nope-nope-00000000/cancel").status_code == 404


def test_cancel_requests_workflow_cancellation(api, fake):
    scan_id = start_scan(api)["scan_id"]
    response = api.post(f"/api/scans/{scan_id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"scan_id": scan_id, "cancel_requested": True}
    assert fake.scans[scan_id].cancelled


def test_client_is_created_once_and_reused(api, factory_calls):
    for _ in range(3):
        start_scan(api)
    assert len(factory_calls) == 1


def test_temporal_ui_base_url_is_configurable(fake):
    async def factory(settings):
        return fake

    settings = load_settings({"TEMPORAL_UI_URL": "https://cloud.temporal.io/", "TEMPORAL_NAMESPACE": "ns.acct"})
    with TestClient(create_app(factory, settings)) as client:
        body = client.post("/api/scans", json={"repo_url": "https://github.com/a/b"}).json()
    assert body["temporal_ui_url"] == f"https://cloud.temporal.io/namespaces/ns.acct/workflows/{body['scan_id']}"


def test_web_directory_is_served_at_root(api):
    response = api.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Temporal Repo Scanner" in response.text


async def test_api_against_test_environment():
    @activity.defn(name="clone_repo")
    async def clone_repo(request: CloneRequest) -> CloneResult:
        if "missing" in request.repo_url:
            raise ApplicationError("not found", type="RepoNotFound")
        return CloneResult(clone_path="/tmp/repo", file_count=1, size_bytes=1)

    @activity.defn(name="prepare_workspace")
    async def prepare_workspace() -> WorkspaceResult:
        return WorkspaceResult(workspace_path="/tmp", skills=[])

    release = asyncio.Event()

    @activity.defn(name="run_agent_scan")
    async def run_agent_scan(request: AgentScanRequest) -> dict:
        while not release.is_set():
            activity.heartbeat("scanning")
            await asyncio.sleep(0.1)
        return dict(REPORT, repo_url=request.repo_url)

    @activity.defn(name="cleanup_workspace")
    async def cleanup_workspace() -> bool:
        return True

    task_queue = str(uuid.uuid4())
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_existing_path=shutil.which("temporal")
    ) as env:
        async def factory(settings):
            return env.client

        app = create_app(factory, load_settings({"TASK_QUEUE": task_queue}))
        @activity.defn(name="get_worker_task_queue")
        async def get_worker_task_queue() -> str:
            return f"{task_queue}-host"

        async with Worker(
            env.client, task_queue=task_queue, workflows=[RepoScanWorkflow], activities=[get_worker_task_queue]
        ), Worker(
            env.client,
            task_queue=f"{task_queue}-host",
            activities=[clone_repo, prepare_workspace, run_agent_scan, cleanup_workspace],
            max_heartbeat_throttle_interval=MAX_HEARTBEAT_THROTTLE,
        ), app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                scan_id = (await http.post("/api/scans", json={"repo_url": "https://github.com/a/b"})).json()["scan_id"]
                body = await poll(http, scan_id, lambda body: body["stage"] == "scanning")
                assert body["report"] is None
                release.set()
                body = await poll(http, scan_id, lambda body: body["stage"] == "done")
                assert body["report"]["repo_url"] == "https://github.com/a/b"

                missing = (await http.post("/api/scans", json={"repo_url": "https://github.com/a/missing"})).json()
                body = await poll(http, missing["scan_id"], lambda body: body["stage"] == "failed")
                assert body["error"] == "Repository not found or not public."

                release.clear()
                cancelled = (await http.post("/api/scans", json={"repo_url": "https://github.com/a/c"})).json()
                await poll(http, cancelled["scan_id"], lambda body: body["stage"] == "scanning")
                response = await http.post(f"/api/scans/{cancelled['scan_id']}/cancel")
                assert response.json()["cancel_requested"] is True
                body = await poll(http, cancelled["scan_id"], lambda body: body["stage"] == "cancelled")
                assert body["error"] == "Scan cancelled"


async def poll(http, scan_id, done, timeout=20.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await http.get(f"/api/scans/{scan_id}")).json()
        if done(body):
            return body
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"Timed out waiting, last body {body}")
        await asyncio.sleep(0.2)
