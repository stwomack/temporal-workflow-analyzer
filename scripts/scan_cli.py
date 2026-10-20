import argparse
import asyncio
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.config import get_settings
from app.local_scan import format_report, run_scan, run_with_embedded_worker
from app.models import ScanProgress
from app.repo_url import InvalidRepoUrlError
from app.temporal_client import connect_client


def print_progress(progress: ScanProgress) -> None:
    latest = progress.events[-1] if progress.events else ""
    print(f"[{progress.stage.value}] {progress.message} {latest}".rstrip(), file=sys.stderr, flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a GitHub repository for Temporal best practice violations.")
    parser.add_argument("repo_url", help="https://github.com/<owner>/<repo>")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="start a throwaway local Temporal dev server and worker")
    mode.add_argument("--use-running-worker", action="store_true", help="only start the workflow; a worker must already poll TASK_QUEUE")
    arguments = parser.parse_args()
    settings = get_settings()

    try:
        if arguments.local:
            async with await WorkflowEnvironment.start_local(
                data_converter=pydantic_data_converter, dev_server_existing_path=shutil.which("temporal")
            ) as environment:
                local_settings = settings.model_copy(update={"task_queue": f"scan-cli-{uuid.uuid4().hex[:8]}"})
                report = await run_with_embedded_worker(environment.client, local_settings, arguments.repo_url, print_progress)
        else:
            client = await connect_client(settings)
            if arguments.use_running_worker:
                report = await run_scan(client, arguments.repo_url, settings.task_queue, print_progress)
            else:
                cli_settings = settings.model_copy(update={"task_queue": f"scan-cli-{uuid.uuid4().hex[:8]}"})
                report = await run_with_embedded_worker(client, cli_settings, arguments.repo_url, print_progress)
    except InvalidRepoUrlError as error:
        print(f"Invalid repository URL: {error}", file=sys.stderr)
        return 2
    except WorkflowFailureError as error:
        print(f"Scan failed: {error.cause}", file=sys.stderr)
        return 1

    print(format_report(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
