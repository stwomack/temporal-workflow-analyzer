import asyncio
import logging
import shutil
import signal
import time
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import cleanup_workspace, clone_repo, prepare_workspace, run_agent_scan
from app.config import Settings, get_settings
from app.temporal_client import connect_client
from app.workflows import RepoScanWorkflow
from app.workspace import warn_about_missing_skills

STALE_SCAN_AGE_SECONDS = 60 * 60
ACTIVITIES = [clone_repo, prepare_workspace, run_agent_scan, cleanup_workspace]

logger = logging.getLogger("app.worker")


def remove_stale_scans(scan_root: Path, max_age_seconds: float = STALE_SCAN_AGE_SECONDS, now: float | None = None) -> list[str]:
    if not scan_root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = []
    for entry in scan_root.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return sorted(removed)


def build_worker(client: Client, settings: Settings) -> Worker:
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[RepoScanWorkflow],
        activities=ACTIVITIES,
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_shutdown_grace_seconds),
    )


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    warn_about_missing_skills()
    removed = remove_stale_scans(Path(settings.scan_root))
    if removed:
        logger.info("Removed %d stale scan directories", len(removed))
    client = await connect_client(settings)
    worker = build_worker(client, settings)
    logger.info(
        "Worker polling task queue %s on %s namespace %s",
        settings.task_queue,
        settings.temporal_address,
        settings.temporal_namespace,
    )
    async with worker:
        await stop.wait()
        logger.info("Shutdown requested, waiting up to %s seconds for running activities", settings.worker_shutdown_grace_seconds)
    logger.info("Worker stopped")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    await run_worker(get_settings(), stop)


if __name__ == "__main__":
    asyncio.run(main())
