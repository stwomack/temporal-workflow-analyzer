from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    import annotated_types
    import pydantic
    from app.activities import (
        AgentScanRequest,
        CloneRequest,
        cleanup_workspace,
        clone_repo,
        prepare_workspace,
        run_agent_scan,
    )
    from app.models import (
        MAX_MESSAGE_LENGTH,
        MAX_PROGRESS_EVENTS,
        PROGRESS_SIGNAL_NAME,
        ScanProgress,
        ScanReport,
        ScanRequest,
        Stage,
    )

CLONE_TIMEOUT = timedelta(minutes=3)
CLONE_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    non_retryable_error_types=["InvalidRepoUrl", "RepoNotFound", "RepoTooLarge"],
)
PREPARE_TIMEOUT = timedelta(minutes=1)
PREPARE_RETRY = RetryPolicy(maximum_attempts=3)
AGENT_TIMEOUT = timedelta(minutes=15)
AGENT_HEARTBEAT_TIMEOUT = timedelta(seconds=60)
AGENT_RETRY = RetryPolicy(maximum_attempts=2, non_retryable_error_types=["MissingApiKey"])
CLEANUP_TIMEOUT = timedelta(minutes=1)
CLEANUP_RETRY = RetryPolicy(maximum_attempts=3)

FRIENDLY_ERRORS = {
    "RepoNotFound": "Repository not found or not public.",
    "MissingApiKey": "The worker has no ANTHROPIC_API_KEY configured.",
    "InvalidApiKey": "The Anthropic API rejected the worker's API key.",
    "AgentBudgetExceeded": "The agent reached its spend limit before finishing.",
    "InvalidAgentOutput": "The agent did not return a valid report.",
}


def describe_failure(error: BaseException) -> str:
    cause = error.cause if isinstance(error, ActivityError) and error.cause else error
    if isinstance(cause, ApplicationError):
        message = FRIENDLY_ERRORS.get(cause.type or "", cause.message)
    elif isinstance(error, ActivityError) and error.cause is not None:
        message = f"Scan step {error.activity_type} failed: {error.cause}"
    else:
        message = str(error) or type(error).__name__
    return message[:MAX_MESSAGE_LENGTH]


def failure_type(error: BaseException) -> str:
    cause = error.cause if isinstance(error, ActivityError) and error.cause else error
    if isinstance(cause, ApplicationError) and cause.type:
        return cause.type
    return "ScanFailed"


@workflow.defn
class RepoScanWorkflow:
    def __init__(self) -> None:
        self.stage = Stage.queued
        self.message = "Waiting for a worker"
        self.events: list[str] = []

    def set_stage(self, stage: Stage, message: str) -> None:
        self.stage = stage
        self.message = message[:MAX_MESSAGE_LENGTH]

    @workflow.signal(name=PROGRESS_SIGNAL_NAME)
    def report_progress(self, event: str) -> None:
        self.events.append(str(event)[:200])
        del self.events[:-MAX_PROGRESS_EVENTS]

    @workflow.query
    def get_progress(self) -> ScanProgress:
        return ScanProgress(stage=self.stage, message=self.message, events=list(self.events))

    @workflow.run
    async def run(self, request: ScanRequest) -> dict:
        try:
            return await self.scan(request)
        except BaseException as error:
            if is_cancelled_exception(error):
                self.set_stage(Stage.cancelled, "Scan cancelled")
                raise
            if not isinstance(error, Exception):
                raise
            self.set_stage(Stage.failed, describe_failure(error))
            raise ApplicationError(self.message, type=failure_type(error), non_retryable=True) from error
        finally:
            await self.cleanup()

    async def scan(self, request: ScanRequest) -> dict:
        self.set_stage(Stage.cloning, "Cloning repository")
        clone = await workflow.execute_activity(
            clone_repo,
            CloneRequest(repo_url=request.repo_url),
            start_to_close_timeout=CLONE_TIMEOUT,
            retry_policy=CLONE_RETRY,
        )
        self.set_stage(Stage.preparing, f"Preparing workspace for {clone.file_count} files")
        await workflow.execute_activity(
            prepare_workspace,
            start_to_close_timeout=PREPARE_TIMEOUT,
            retry_policy=PREPARE_RETRY,
        )
        self.set_stage(Stage.scanning, "Agent is reviewing the repository")
        raw_report = await workflow.execute_activity(
            run_agent_scan,
            AgentScanRequest(repo_url=request.repo_url),
            start_to_close_timeout=AGENT_TIMEOUT,
            heartbeat_timeout=AGENT_HEARTBEAT_TIMEOUT,
            retry_policy=AGENT_RETRY,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self.set_stage(Stage.parsing, "Validating report")
        report = ScanReport.model_validate(raw_report)
        self.set_stage(Stage.done, f"Found {len(report.findings)} findings")
        return report.model_dump(mode="json")

    async def cleanup(self) -> None:
        try:
            await workflow.execute_activity(
                cleanup_workspace,
                start_to_close_timeout=CLEANUP_TIMEOUT,
                retry_policy=CLEANUP_RETRY,
            )
        except ActivityError as error:
            workflow.logger.warning("Workspace cleanup failed: %s", error.cause or error)
