import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
)
from pydantic import ValidationError

from app.agent.prompt import DOCS_UNAVAILABLE_NOTE, REPO_DIRECTORY, SYSTEM_PROMPT, build_repair_prompt, build_user_prompt
from app.models import MAX_SUMMARY_LENGTH, ScanReport

DOCS_SERVER_NAME = "temporal-docs"
DOCS_SERVER_URL = "https://temporal.mcp.kapa.ai"
GITHUB_SERVER_NAME = "github"
GITHUB_SERVER_URL = "https://api.githubcopilot.com/mcp"
GITHUB_READ_TOOLS = (
    "get_file_contents",
    "search_code",
    "get_repository",
    "list_commits",
    "get_commit",
)
BUILTIN_TOOLS = ["Read", "Grep", "Glob", "Skill"]
FORBIDDEN_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"]
REPAIR_MAX_TURNS = 3
PROGRESS_MIN_INTERVAL_SECONDS = 2.0
PROGRESS_MAX_EVENTS = 50
PROGRESS_SEND_TIMEOUT_SECONDS = 5.0
MAX_EVENT_TEXT = 60
MAX_STEP_LENGTH = 120
CANCEL_GRACE_SECONDS = 25.0
RETRYABLE_API_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

logger = logging.getLogger(__name__)

QueryFunction = Callable[..., AsyncIterator[Any]]


class AgentFailure(Exception):
    def __init__(self, message: str, error_type: str, non_retryable: bool = False):
        super().__init__(message)
        self.error_type = error_type
        self.non_retryable = non_retryable


class InvalidAgentOutput(Exception):
    pass


def build_options(
    workspace: Path,
    model: str,
    api_key: str,
    skills: list[str],
    max_turns: int,
    max_budget_usd: float,
    github_token: str | None,
) -> ClaudeAgentOptions:
    mcp_servers: dict[str, Any] = {DOCS_SERVER_NAME: {"type": "http", "url": DOCS_SERVER_URL}}
    allowed_tools = ["Read", "Grep", "Glob", f"mcp__{DOCS_SERVER_NAME}"]
    if github_token:
        mcp_servers[GITHUB_SERVER_NAME] = {
            "type": "http",
            "url": GITHUB_SERVER_URL,
            "headers": {"Authorization": f"Bearer {github_token}", "X-MCP-Readonly": "true"},
        }
        allowed_tools.extend(f"mcp__{GITHUB_SERVER_NAME}__{tool}" for tool in GITHUB_READ_TOOLS)
    return ClaudeAgentOptions(
        model=model,
        cwd=str(workspace),
        system_prompt=SYSTEM_PROMPT,
        setting_sources=["project"],
        skills=skills,
        tools=list(BUILTIN_TOOLS),
        allowed_tools=allowed_tools,
        disallowed_tools=list(FORBIDDEN_TOOLS),
        permission_mode="dontAsk",
        mcp_servers=mcp_servers,
        strict_mcp_config=True,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        env={"ANTHROPIC_API_KEY": api_key},
    )


def clean_text(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    text = CONTROL_CHARACTERS.sub(" ", str(value))
    text = " ".join(text.split())
    return text[:limit]


def relative_to_repo(path: str, workspace: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        for base in (workspace.resolve(strict=False) / REPO_DIRECTORY, workspace.resolve(strict=False)):
            if resolved.is_relative_to(base):
                return resolved.relative_to(base).as_posix()
        return candidate.name
    text = candidate.as_posix()
    return text[len(REPO_DIRECTORY) + 1:] if text.startswith(REPO_DIRECTORY + "/") else text


def describe_tool_use(block: ToolUseBlock, workspace: Path) -> str:
    arguments = block.input or {}
    if block.name == "Read":
        return f"file read {clean_text(relative_to_repo(str(arguments.get('file_path', '')), workspace), 100)}"
    if block.name == "Grep":
        return f"search {clean_text(arguments.get('pattern', ''))}"
    if block.name == "Glob":
        return f"list files {clean_text(arguments.get('pattern', ''))}"
    if block.name == "Skill":
        return f"skill loaded {clean_text(arguments.get('skill') or arguments.get('name') or '')}"
    if block.name.startswith(f"mcp__{DOCS_SERVER_NAME}__"):
        return f"docs lookup {clean_text(arguments.get('query', ''))}"
    if block.name.startswith(f"mcp__{GITHUB_SERVER_NAME}__"):
        return f"github {clean_text(block.name.rsplit('__', 1)[-1])}"
    return f"tool {clean_text(block.name)}"


def extract_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[A-Za-z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end < start:
        raise InvalidAgentOutput("The final message did not contain a JSON object")
    try:
        return json.loads(stripped[start:end + 1])
    except json.JSONDecodeError as error:
        raise InvalidAgentOutput(f"The final message was not valid JSON: {error}") from error


def parse_report(text: str | None, repo_url: str, docs_available: bool) -> ScanReport:
    if not text or not text.strip():
        raise InvalidAgentOutput("The agent finished without a final message")
    data = extract_json_object(text)
    if not isinstance(data, dict):
        raise InvalidAgentOutput("The final JSON value must be an object")
    data["repo_url"] = repo_url
    try:
        report = ScanReport.model_validate(data)
    except ValidationError as error:
        raise InvalidAgentOutput(str(error)) from error
    if not docs_available:
        summary = f"{DOCS_UNAVAILABLE_NOTE} {report.summary}".strip()[:MAX_SUMMARY_LENGTH].rstrip()
        report = ScanReport.model_validate({**report.model_dump(mode="json"), "summary": summary})
    return report


class ProgressReporter:
    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        min_interval: float | None = None,
        max_events: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.send = send
        self.min_interval = PROGRESS_MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        self.max_events = PROGRESS_MAX_EVENTS if max_events is None else max_events
        self.clock = clock
        self.sent = 0
        self.last_sent_at: float | None = None

    async def report(self, event: str) -> bool:
        if self.sent >= self.max_events:
            return False
        now = self.clock()
        if self.last_sent_at is not None and now - self.last_sent_at < self.min_interval:
            return False
        self.sent += 1
        self.last_sent_at = now
        try:
            await asyncio.wait_for(self.send(event[:MAX_STEP_LENGTH]), timeout=PROGRESS_SEND_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning("Progress signal failed: %s", type(error).__name__)
        return True


class AgentScan:
    def __init__(
        self,
        options: ClaudeAgentOptions,
        repo_url: str,
        reporter: ProgressReporter,
        heartbeat: Callable[[str], None],
        query_function: QueryFunction,
    ):
        self.options = options
        self.repo_url = repo_url
        self.reporter = reporter
        self.heartbeat = heartbeat
        self.query_function = query_function
        self.workspace = Path(str(options.cwd))
        self.latest_step = "starting agent"
        self.docs_available = False

    async def record(self, step: str, report: bool = True) -> None:
        self.latest_step = clean_text(step, MAX_STEP_LENGTH)
        self.heartbeat(self.latest_step)
        if report:
            await self.reporter.report(self.latest_step)

    async def handle_message(self, message: Any) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            servers = {server.get("name"): server.get("status") for server in message.data.get("mcp_servers", [])}
            self.docs_available = servers.get(DOCS_SERVER_NAME) == "connected"
            await self.record("agent started" if self.docs_available else "agent started, docs lookup unavailable")
            return
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    await self.record(describe_tool_use(block, self.workspace))
                    return
            await self.record("thinking", report=False)
            return
        if isinstance(message, ResultMessage):
            await self.record("agent finished")
            return
        self.heartbeat(self.latest_step)

    async def run_query(self, prompt: str, options: ClaudeAgentOptions) -> ResultMessage | None:
        result = None
        try:
            async with aclosing(self.query_function(prompt=prompt, options=options)) as messages:
                async for message in messages:
                    await self.handle_message(message)
                    if isinstance(message, ResultMessage):
                        result = message
        except CLINotFoundError as error:
            raise AgentFailure(f"Claude Code CLI not found: {error}", "AgentCliMissing", non_retryable=True) from error
        except ClaudeSDKError as error:
            raise AgentFailure(f"Agent run failed: {clean_text(error, 200)}", "AgentFailed") from error
        return result

    def final_text(self, result: ResultMessage | None) -> str | None:
        if result is None:
            raise AgentFailure("The agent stopped without a result", "AgentFailed")
        if result.subtype == "error_max_budget_usd":
            raise AgentFailure("The agent exceeded its spend limit", "AgentBudgetExceeded", non_retryable=True)
        if result.subtype == "error_max_turns":
            raise InvalidAgentOutput("The turn limit was reached before the final report was written")
        if result.is_error:
            status = result.api_error_status
            if status in (401, 403):
                raise AgentFailure(f"Anthropic API rejected the credentials ({status})", "InvalidApiKey", True)
            non_retryable = status is not None and status not in RETRYABLE_API_STATUSES
            raise AgentFailure(
                f"Agent run failed ({result.subtype}, status {status})", "AgentFailed", non_retryable=non_retryable
            )
        return result.result

    async def run(self) -> ScanReport:
        result = await self.run_query(build_user_prompt(self.repo_url), self.options)
        try:
            return parse_report(self.final_text(result), self.repo_url, self.docs_available)
        except InvalidAgentOutput as first_error:
            if result is None or not result.session_id:
                raise AgentFailure(str(first_error)[:300], "InvalidAgentOutput") from first_error
            await self.record("repairing report")
            repair_options = replace(self.options, resume=result.session_id, max_turns=REPAIR_MAX_TURNS)
            repaired = await self.run_query(build_repair_prompt(str(first_error)), repair_options)
            try:
                return parse_report(self.final_text(repaired), self.repo_url, self.docs_available)
            except InvalidAgentOutput as second_error:
                raise AgentFailure(
                    f"Agent output was invalid after one repair attempt: {str(second_error)[:300]}",
                    "InvalidAgentOutput",
                ) from second_error


async def heartbeat_forever(scan: AgentScan, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        scan.heartbeat(scan.latest_step)


async def run_with_clean_cancellation(work: Callable[[], Awaitable[Any]]) -> Any:
    scope = anyio.CancelScope()

    async def guarded() -> Any:
        with scope:
            return await work()
        raise asyncio.CancelledError()

    task = asyncio.create_task(guarded())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        scope.cancel()
        await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
        if task.done():
            if not task.cancelled():
                task.exception()
        else:
            task.cancel()
        raise
