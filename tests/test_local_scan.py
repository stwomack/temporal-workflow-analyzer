from app.local_scan import format_report
from app.models import ScanReport


def test_format_report_is_plain_text():
    report = ScanReport.model_validate({
        "repo_url": "https://github.com/a/b",
        "detected_languages": ["TypeScript"],
        "summary": "Two problems.",
        "findings": [
            {"rank": 1, "severity": "critical", "category": "idempotency", "title": "Payment retried",
             "file": "src/activities.ts", "line": 10, "problem": "Charges twice.", "fix": "Add an idempotency key.",
             "doc_url": "https://docs.temporal.io/activities"},
            {"rank": 2, "severity": "medium", "category": "other", "title": "Swallowed error",
             "file": "src/workflows.ts", "line": None, "problem": "Errors hidden.", "fix": "Rethrow.", "doc_url": None},
        ],
        "truncated": True,
    })
    text = format_report(report)
    assert "1. [critical] [idempotency] Payment retried" in text
    assert "Location: src/activities.ts:10" in text
    assert "Location: src/workflows.ts\n" in text
    assert "Docs: https://docs.temporal.io/activities" in text
    assert "More issues exist than are listed." in text
    assert text.isascii()


def test_format_report_without_findings():
    report = ScanReport(repo_url="https://github.com/a/b", detected_languages=[], summary="No Temporal code.")
    assert "No findings." in format_report(report)
    assert "Languages: none detected" in format_report(report)
