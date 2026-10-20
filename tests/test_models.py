import pytest
from pydantic import ValidationError

from app.models import (
    MAX_FINDINGS,
    MAX_PROGRESS_EVENTS,
    Finding,
    ScanProgress,
    ScanReport,
    ScanRequest,
    Severity,
    Stage,
)


def make_finding(rank: int, severity: str = "high", **overrides) -> dict:
    finding = {
        "rank": rank,
        "severity": severity,
        "category": "timeouts",
        "title": "Activity has no start_to_close timeout",
        "file": "src/workflows.py",
        "line": 42,
        "problem": "The activity is scheduled without any timeout.",
        "fix": "Set start_to_close_timeout on the activity call.",
        "doc_url": None,
    }
    finding.update(overrides)
    return finding


def make_report(findings: list[dict], **overrides) -> dict:
    report = {
        "repo_url": "https://github.com/example/demo",
        "detected_languages": ["python"],
        "summary": "Two problems found.",
        "findings": findings,
        "truncated": False,
    }
    report.update(overrides)
    return report


def test_valid_report_round_trips_through_dict():
    report = ScanReport.model_validate(
        make_report([make_finding(1, "critical"), make_finding(2, "high"), make_finding(3, "medium")])
    )
    assert ScanReport.model_validate(report.model_dump(mode="json")) == report
    assert report.findings[0].severity is Severity.critical


def test_empty_findings_are_allowed():
    report = ScanReport.model_validate(make_report([], summary="No Temporal code found."))
    assert report.findings == []


def test_seven_findings_are_allowed():
    findings = [make_finding(rank) for rank in range(1, MAX_FINDINGS + 1)]
    assert len(ScanReport.model_validate(make_report(findings)).findings) == MAX_FINDINGS


def test_more_than_seven_findings_are_rejected():
    findings = [make_finding(rank) for rank in range(1, MAX_FINDINGS + 2)]
    with pytest.raises(ValidationError, match="at most 7"):
        ScanReport.model_validate(make_report(findings))


def test_lower_severity_before_higher_severity_is_rejected():
    with pytest.raises(ValidationError, match="ordered by severity"):
        ScanReport.model_validate(make_report([make_finding(1, "medium"), make_finding(2, "critical")]))


def test_ranks_out_of_order_within_same_severity_are_rejected():
    with pytest.raises(ValidationError, match="ordered by severity"):
        ScanReport.model_validate(make_report([make_finding(2, "high"), make_finding(1, "high")]))


def test_ranks_that_contradict_severity_are_rejected():
    with pytest.raises(ValidationError, match="ranks must increase"):
        ScanReport.model_validate(make_report([make_finding(2, "critical"), make_finding(1, "high")]))


def test_duplicate_ranks_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        ScanReport.model_validate(make_report([make_finding(1), make_finding(1)]))


def test_title_longer_than_80_characters_is_rejected():
    Finding.model_validate(make_finding(1, title="x" * 80))
    with pytest.raises(ValidationError):
        Finding.model_validate(make_finding(1, title="x" * 81))


def test_summary_longer_than_200_characters_is_rejected():
    ScanReport.model_validate(make_report([], summary="s" * 200))
    with pytest.raises(ValidationError):
        ScanReport.model_validate(make_report([], summary="s" * 201))


def test_unknown_severity_and_category_are_rejected():
    with pytest.raises(ValidationError):
        Finding.model_validate(make_finding(1, severity="low"))
    with pytest.raises(ValidationError):
        Finding.model_validate(make_finding(1, category="style"))


def test_rank_must_be_positive():
    with pytest.raises(ValidationError):
        Finding.model_validate(make_finding(0))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../x.py", "C:/repo/x.py"])
def test_file_outside_repo_is_rejected(path):
    with pytest.raises(ValidationError, match="relative"):
        Finding.model_validate(make_finding(1, file=path))


def test_file_leading_dot_slash_is_normalized():
    assert Finding.model_validate(make_finding(1, file="./src/app.py")).file == "src/app.py"


def test_line_may_be_null():
    assert Finding.model_validate(make_finding(1, line=None)).line is None


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        Finding.model_validate(make_finding(1, confidence="high"))


def test_scan_request_requires_repo_url():
    assert ScanRequest(repo_url="https://github.com/a/b").repo_url == "https://github.com/a/b"
    with pytest.raises(ValidationError):
        ScanRequest(repo_url="")


def test_progress_defaults_to_queued():
    progress = ScanProgress()
    assert progress.stage is Stage.queued
    assert progress.events == []


def test_progress_keeps_only_most_recent_events():
    events = [f"event {index}" for index in range(MAX_PROGRESS_EVENTS + 10)]
    progress = ScanProgress(stage="scanning", message="Scanning", events=events)
    assert len(progress.events) == MAX_PROGRESS_EVENTS
    assert progress.events[0] == "event 10"
    assert progress.events[-1] == events[-1]


def test_progress_rejects_long_event_and_message():
    with pytest.raises(ValidationError):
        ScanProgress(events=["e" * 201])
    with pytest.raises(ValidationError):
        ScanProgress(message="m" * 201)


def test_progress_rejects_unknown_stage():
    with pytest.raises(ValidationError):
        ScanProgress(stage="paused")
