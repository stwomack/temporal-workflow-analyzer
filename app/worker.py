import asyncio
import logging
import re
import shutil
import signal
import socket
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import WorkerQueueActivities, cleanup_workspace, clone_repo, prepare_workspace, run_agent_scan
from app.config import Settings, get_settings
from app.temporal_client import connect_client
from app.workflows import RepoScanWorkflow
from app.workspace import warn_about_missing_skills

STALE_SCAN_AGE_SECONDS = 60 * 60
MAX_HEARTBEAT_THROTTLE = timedelta(seconds=5)
ACTIVITIES = [clone_repo, prepare_workspace, run_agent_scan, cleanup_workspace]
UNSAFE_QUEUE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")

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


def host_task_queue(settings: Settings) -> str:
    host = settings.worker_host_id or socket.gethostname()
    return f"{settings.task_queue}-host-{UNSAFE_QUEUE_CHARACTERS.sub('-', host)}"


def build_workers(client: Client, settings: Settings) -> list[Worker]:
    shutdown_grace = timedelta(seconds=settings.worker_shutdown_grace_seconds)
    queue = host_task_queue(settings)
    shared = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[RepoScanWorkflow],
        activities=[WorkerQueueActivities(queue).get_worker_task_queue],
        graceful_shutdown_timeout=shutdown_grace,
    )
    host = Worker(
        client,
        task_queue=queue,
        activities=ACTIVITIES,
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        graceful_shutdown_timeout=shutdown_grace,
        max_heartbeat_throttle_interval=MAX_HEARTBEAT_THROTTLE,
    )
    return [shared, host]


@asynccontextmanager
async def running_workers(client: Client, settings: Settings):
    async with AsyncExitStack() as stack:
        for worker in build_workers(client, settings):
            await stack.enter_async_context(worker)
        yield


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    warn_about_missing_skills()
    removed = remove_stale_scans(Path(settings.scan_root))
    if removed:
        logger.info("Removed %d stale scan directories", len(removed))
    client = await connect_client(settings)
    logger.info(
        "Worker polling task queue %s and host queue %s on %s namespace %s",
        settings.task_queue,
        host_task_queue(settings),
        settings.temporal_address,
        settings.temporal_namespace,
    )
    async with running_workers(client, settings):
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
