import asyncio
from collections.abc import Callable

from temporalio.client import Client

from app.api import new_scan_id
from app.config import Settings
from app.models import ScanProgress, ScanReport, ScanRequest, Stage
from app.repo_url import parse_github_repo_url
from app.worker import build_worker
from app.workflows import RepoScanWorkflow

POLL_INTERVAL_SECONDS = 1.5
FINAL_STAGES = {Stage.done, Stage.failed, Stage.cancelled}


async def run_scan(
    client: Client,
    repo_url: str,
    task_queue: str,
    on_progress: Callable[[ScanProgress], None] | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> ScanReport:
    repo = parse_github_repo_url(repo_url)
    handle = await client.start_workflow(
        RepoScanWorkflow.run,
        ScanRequest(repo_url=f"https://github.com/{repo.owner}/{repo.repo}"),
        id=new_scan_id(repo.owner, repo.repo),
        task_queue=task_queue,
    )
    result_task = asyncio.create_task(handle.result())
    seen_events = 0
    last_stage = None
    while not result_task.done():
        try:
            progress = await handle.query(RepoScanWorkflow.get_progress)
        except Exception:
            progress = None
        if progress and on_progress and (progress.stage != last_stage or len(progress.events) != seen_events):
            on_progress(progress)
            last_stage = progress.stage
            seen_events = len(progress.events)
        if progress and progress.stage in FINAL_STAGES:
            break
        await asyncio.wait({result_task}, timeout=poll_interval)
    return ScanReport.model_validate(await result_task)


async def run_with_embedded_worker(
    client: Client,
    settings: Settings,
    repo_url: str,
    on_progress: Callable[[ScanProgress], None] | None = None,
) -> ScanReport:
    async with build_worker(client, settings):
        return await run_scan(client, repo_url, settings.task_queue, on_progress)


def format_report(report: ScanReport) -> str:
    lines = [
        f"Repository: {report.repo_url}",
        f"Languages: {', '.join(report.detected_languages) or 'none detected'}",
        f"Summary: {report.summary}",
        "",
    ]
    if not report.findings:
        lines.append("No findings.")
    for finding in report.findings:
        location = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        lines.extend([
            f"{finding.rank}. [{finding.severity.value}] [{finding.category.value}] {finding.title}",
            f"   Location: {location}",
            f"   Problem: {finding.problem}",
            f"   Fix: {finding.fix}",
        ])
        if finding.doc_url:
            lines.append(f"   Docs: {finding.doc_url}")
        lines.append("")
    if report.truncated:
        lines.append("More issues exist than are listed.")
    return "\n".join(lines).rstrip() + "\n"
