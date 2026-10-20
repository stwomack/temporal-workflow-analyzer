from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

MAX_FINDINGS = 7
MAX_PROGRESS_EVENTS = 25
MAX_EVENT_LENGTH = 200
MAX_TITLE_LENGTH = 80
MAX_SUMMARY_LENGTH = 200
MAX_SENTENCE_LENGTH = 400
MAX_MESSAGE_LENGTH = 200
PROGRESS_SIGNAL_NAME = "report_progress"


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"


SEVERITY_ORDER = {Severity.critical: 0, Severity.high: 1, Severity.medium: 2}


class Category(str, Enum):
    non_determinism = "non_determinism"
    retry_policy = "retry_policy"
    timeouts = "timeouts"
    idempotency = "idempotency"
    payload_size = "payload_size"
    heartbeats = "heartbeats"
    versioning = "versioning"
    continue_as_new = "continue_as_new"
    other = "other"


class Stage(str, Enum):
    queued = "queued"
    cloning = "cloning"
    preparing = "preparing"
    scanning = "scanning"
    parsing = "parsing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ProgressEvent = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_EVENT_LENGTH)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanRequest(StrictModel):
    repo_url: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class Finding(StrictModel):
    rank: int = Field(ge=1)
    severity: Severity
    category: Category
    title: Annotated[ShortText, StringConstraints(max_length=MAX_TITLE_LENGTH)]
    file: Annotated[ShortText, StringConstraints(max_length=300)]
    line: int | None = Field(default=None, ge=1)
    problem: Annotated[ShortText, StringConstraints(max_length=MAX_SENTENCE_LENGTH)]
    fix: Annotated[ShortText, StringConstraints(max_length=MAX_SENTENCE_LENGTH)]
    doc_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None

    @field_validator("file")
    @classmethod
    def file_must_be_relative_to_repo_root(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("/") or ".." in normalized.split("/") or ":" in normalized.split("/")[0]:
            raise ValueError("file must be a path relative to the repository root")
        return normalized


class ScanReport(StrictModel):
    repo_url: str
    detected_languages: list[Annotated[ShortText, StringConstraints(max_length=40)]] = Field(default_factory=list)
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_SUMMARY_LENGTH)]
    findings: list[Finding] = Field(default_factory=list)
    truncated: bool = False

    @field_validator("findings")
    @classmethod
    def findings_must_be_capped_and_ordered(cls, findings: list[Finding]) -> list[Finding]:
        if len(findings) > MAX_FINDINGS:
            raise ValueError(f"at most {MAX_FINDINGS} findings are allowed, got {len(findings)}")
        ranks = [finding.rank for finding in findings]
        if len(set(ranks)) != len(ranks):
            raise ValueError("finding ranks must be unique")
        expected = sorted(findings, key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.rank))
        if findings != expected:
            raise ValueError("findings must be ordered by severity, then by rank")
        if ranks != sorted(ranks):
            raise ValueError("finding ranks must increase from most to least important")
        return findings


class ScanProgress(StrictModel):
    stage: Stage = Stage.queued
    message: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_MESSAGE_LENGTH)] = ""
    events: list[ProgressEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_most_recent_events(self) -> "ScanProgress":
        if len(self.events) > MAX_PROGRESS_EVENTS:
            self.events = self.events[-MAX_PROGRESS_EVENTS:]
        return self
