import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from app.config import Settings, get_settings
from app.models import MAX_MESSAGE_LENGTH, ScanProgress, ScanReport, ScanRequest, Stage
from app.repo_url import InvalidRepoUrlError, parse_github_repo_url
from app.temporal_client import connect_client
from app.workflows import RepoScanWorkflow

WEB_DIRECTORY = Path(__file__).resolve().parent.parent / "web"
CLOSED_FAILURE_MESSAGES = {
    WorkflowExecutionStatus.CANCELED: (Stage.cancelled, "Scan cancelled"),
    WorkflowExecutionStatus.TERMINATED: (Stage.failed, "Scan was terminated"),
    WorkflowExecutionStatus.TIMED_OUT: (Stage.failed, "Scan timed out"),
}

ClientFactory = Callable[[Settings], Awaitable[Client]]


class StartScanResponse(BaseModel):
    scan_id: str
    temporal_ui_url: str


class ScanStatusResponse(BaseModel):
    scan_id: str
    stage: Stage
    message: str
    events: list[str]
    report: ScanReport | None = None
    error: str | None = None
    temporal_ui_url: str


class CancelScanResponse(BaseModel):
    scan_id: str
    cancel_requested: bool


def new_scan_id(owner: str, repo: str) -> str:
    return f"scan-{owner}-{repo}-{secrets.token_hex(4)}"


def temporal_ui_url(settings: Settings, scan_id: str) -> str:
    return f"{settings.temporal_ui_url.rstrip('/')}/namespaces/{settings.temporal_namespace}/workflows/{scan_id}"


def failure_message(error: BaseException) -> str:
    cause = getattr(error, "cause", None) or error
    if isinstance(cause, ApplicationError):
        return cause.message[:MAX_MESSAGE_LENGTH]
    return (str(cause) or "Scan failed")[:MAX_MESSAGE_LENGTH]


def create_app(client_factory: ClientFactory = connect_client, settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.temporal_client = await client_factory(resolved_settings)
        yield

    app = FastAPI(title="Temporal Repo Scanner", lifespan=lifespan)

    def client() -> Client:
        return app.state.temporal_client

    async def latest_progress(handle) -> ScanProgress | None:
        try:
            return await handle.query(RepoScanWorkflow.get_progress)
        except Exception:
            return None

    async def describe_or_404(handle):
        try:
            return await handle.describe()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(status_code=404, detail="Scan not found") from error
            raise

    @app.post("/api/scans", response_model=StartScanResponse)
    async def start_scan(request: ScanRequest) -> StartScanResponse:
        try:
            repo = parse_github_repo_url(request.repo_url)
        except InvalidRepoUrlError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        scan_id = new_scan_id(repo.owner, repo.repo)
        await client().start_workflow(
            RepoScanWorkflow.run,
            ScanRequest(repo_url=f"https://github.com/{repo.owner}/{repo.repo}"),
            id=scan_id,
            task_queue=resolved_settings.task_queue,
        )
        return StartScanResponse(scan_id=scan_id, temporal_ui_url=temporal_ui_url(resolved_settings, scan_id))

    @app.get("/api/scans/{scan_id}", response_model=ScanStatusResponse)
    async def get_scan(scan_id: str) -> ScanStatusResponse:
        handle = client().get_workflow_handle(scan_id)
        description = await describe_or_404(handle)
        progress = await latest_progress(handle)
        events = progress.events if progress else []
        base = {"scan_id": scan_id, "events": events, "temporal_ui_url": temporal_ui_url(resolved_settings, scan_id)}

        if description.status == WorkflowExecutionStatus.RUNNING:
            current = progress or ScanProgress(stage=Stage.queued, message="Waiting for a worker")
            return ScanStatusResponse(**base, stage=current.stage, message=current.message)

        if description.status == WorkflowExecutionStatus.COMPLETED:
            report = ScanReport.model_validate(await handle.result())
            message = progress.message if progress else f"Found {len(report.findings)} findings"
            return ScanStatusResponse(**base, stage=Stage.done, message=message, report=report)

        if description.status in CLOSED_FAILURE_MESSAGES:
            stage, message = CLOSED_FAILURE_MESSAGES[description.status]
            return ScanStatusResponse(**base, stage=stage, message=message, error=message)

        try:
            await handle.result()
            error_text = "Scan failed"
        except WorkflowFailureError as error:
            error_text = failure_message(error)
        return ScanStatusResponse(**base, stage=Stage.failed, message=error_text, error=error_text)

    @app.post("/api/scans/{scan_id}/cancel", response_model=CancelScanResponse)
    async def cancel_scan(scan_id: str) -> CancelScanResponse:
        handle = client().get_workflow_handle(scan_id)
        await describe_or_404(handle)
        await handle.cancel()
        return CancelScanResponse(scan_id=scan_id, cancel_requested=True)

    app.mount("/", StaticFiles(directory=WEB_DIRECTORY, html=True), name="web")
    return app


app = create_app()
