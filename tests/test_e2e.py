import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.config import get_settings
from app.local_scan import format_report, run_with_embedded_worker
from app.models import SEVERITY_ORDER, ScanReport
from app.repo_url import parse_github_repo_url

DEMO_REPO_URL = os.environ.get("DEMO_REPO_URL")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (DEMO_REPO_URL and os.environ.get("ANTHROPIC_API_KEY")),
        reason="needs DEMO_REPO_URL and ANTHROPIC_API_KEY",
    ),
]


@pytest.fixture
def reference_clone(tmp_path: Path) -> Path:
    destination = tmp_path / "reference"
    clone_url = parse_github_repo_url(DEMO_REPO_URL).clone_url
    subprocess.run(["git", "clone", "--depth", "1", "--", clone_url, str(destination)], check=True, capture_output=True)
    return destination


async def test_full_scan_of_demo_repo(reference_clone, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCAN_ROOT", str(tmp_path / "scans"))
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"task_queue": f"e2e-{uuid.uuid4().hex[:8]}"})
    events: list[str] = []

    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_existing_path=shutil.which("temporal")
    ) as environment:
        report = await run_with_embedded_worker(
            environment.client, settings, DEMO_REPO_URL, lambda progress: events.extend(progress.events[-1:])
        )
    get_settings.cache_clear()

    assert ScanReport.model_validate(report.model_dump(mode="json")) == report
    assert 1 <= len(report.findings) <= 7
    severities = [SEVERITY_ORDER[finding.severity] for finding in report.findings]
    assert severities == sorted(severities)
    for finding in report.findings:
        assert (reference_clone / finding.file).is_file(), finding.file
    assert any(finding.severity.value in ("critical", "high") for finding in report.findings)
    assert not (tmp_path / "scans").exists() or not any((tmp_path / "scans").iterdir())

    with capsys.disabled():
        print("\n" + format_report(report))
        print(f"progress events seen: {len(events)}")
