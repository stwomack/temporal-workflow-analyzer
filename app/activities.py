import asyncio
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import get_settings
from app.repo_url import InvalidRepoUrlError, parse_github_repo_url
from app.workspace import REPO_DIRECTORY_NAME, WorkspaceRepoMissingError, prepare_workspace_directory

MAX_REPO_BYTES = 200 * 1024 * 1024
MAX_REPO_FILES = 20_000
GIT_CLONE_TIMEOUT_SECONDS = 150
MAX_GIT_ERROR_LENGTH = 300
CLONE_MARKER_NAME = "clone-complete.json"
WORKFLOW_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")

NOT_FOUND_MARKERS = (
    "repository not found",
    "could not read username",
    "authentication failed",
    "does not appear to be a git repository",
    "not found",
)
NETWORK_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection reset",
    "operation timed out",
    "unable to access",
    "early eof",
    "the remote end hung up",
)


@dataclass(frozen=True)
class CloneRequest:
    repo_url: str


@dataclass(frozen=True)
class CloneResult:
    clone_path: str
    file_count: int
    size_bytes: int


@dataclass(frozen=True)
class WorkspaceResult:
    workspace_path: str
    skills: list[str]


def workspace_directory(workflow_id: str) -> Path:
    if not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
        raise ApplicationError(
            "Workflow id is not safe to use as a directory name",
            type="InvalidWorkflowId",
            non_retryable=True,
        )
    return Path(get_settings().scan_root) / workflow_id


def measure_tree(root: Path) -> tuple[int, int]:
    file_count = 0
    size_bytes = 0
    for directory, subdirectories, files in os.walk(root):
        inside_git = Path(directory).relative_to(root).parts[:1] == (".git",)
        for name in files:
            size_bytes += os.lstat(os.path.join(directory, name)).st_size
            if not inside_git:
                file_count += 1
    return file_count, size_bytes


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def read_marker(marker: Path, clone_url: str, repo_directory: Path) -> CloneResult | None:
    if not marker.is_file() or not repo_directory.is_dir():
        return None
    try:
        data = json.loads(marker.read_text())
        if data.pop("clone_url") != clone_url:
            return None
        result = CloneResult(**data)
    except (ValueError, KeyError, TypeError):
        return None
    if result.clone_path != str(repo_directory):
        return None
    return result


def classify_git_failure(stderr: str) -> ApplicationError:
    lowered = stderr.lower()
    summary = stderr.strip().splitlines()[-1][:MAX_GIT_ERROR_LENGTH] if stderr.strip() else "git clone failed"
    if any(marker in lowered for marker in NETWORK_MARKERS) and "not found" not in lowered:
        return ApplicationError(f"Network error while cloning: {summary}", type="CloneNetworkError")
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return ApplicationError(
            "Repository not found or not public",
            type="RepoNotFound",
            non_retryable=True,
        )
    return ApplicationError(f"git clone failed: {summary}", type="CloneFailed")


def git_clone_command(clone_url: str, destination: Path) -> list[str]:
    return [
        "git",
        "-c",
        "core.symlinks=false",
        "-c",
        "core.hooksPath=/dev/null",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        "--",
        clone_url,
        str(destination),
    ]


def git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


async def run_git_clone(clone_url: str, destination: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        *git_clone_command(clone_url, destination),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=git_environment(),
        start_new_session=True,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_CLONE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await terminate_process(process)
        raise ApplicationError(
            f"git clone did not finish within {GIT_CLONE_TIMEOUT_SECONDS} seconds",
            type="CloneTimeout",
        )
    except asyncio.CancelledError:
        await terminate_process(process)
        raise
    if process.returncode != 0:
        raise classify_git_failure(stderr.decode(errors="replace"))


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, 9)
    except ProcessLookupError:
        pass
    await process.wait()


@activity.defn
async def clone_repo(request: CloneRequest) -> CloneResult:
    try:
        repo = parse_github_repo_url(request.repo_url)
    except InvalidRepoUrlError as error:
        raise ApplicationError(str(error), type="InvalidRepoUrl", non_retryable=True) from error

    workspace = workspace_directory(activity.info().workflow_id or "")
    repo_directory = workspace / REPO_DIRECTORY_NAME
    marker = workspace / CLONE_MARKER_NAME

    existing = await asyncio.to_thread(read_marker, marker, repo.clone_url, repo_directory)
    if existing is not None:
        activity.logger.info("Clone already complete, reusing %s", repo_directory)
        return existing

    await asyncio.to_thread(remove_path, marker)
    await asyncio.to_thread(remove_path, repo_directory)
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)

    await run_git_clone(repo.clone_url, repo_directory)

    file_count, size_bytes = await asyncio.to_thread(measure_tree, repo_directory)
    if size_bytes > MAX_REPO_BYTES or file_count > MAX_REPO_FILES:
        await asyncio.to_thread(remove_path, repo_directory)
        raise ApplicationError(
            f"Repository is too large to scan ({file_count} files, {size_bytes // (1024 * 1024)} MB); "
            f"limits are {MAX_REPO_FILES} files and {MAX_REPO_BYTES // (1024 * 1024)} MB",
            type="RepoTooLarge",
            non_retryable=True,
        )

    result = CloneResult(clone_path=str(repo_directory), file_count=file_count, size_bytes=size_bytes)
    marker_data = {"clone_url": repo.clone_url, **asdict(result)}
    await asyncio.to_thread(marker.write_text, json.dumps(marker_data))
    return result


@activity.defn
async def prepare_workspace() -> WorkspaceResult:
    workspace = workspace_directory(activity.info().workflow_id or "")
    try:
        skills = await asyncio.to_thread(prepare_workspace_directory, workspace)
    except WorkspaceRepoMissingError as error:
        raise ApplicationError(str(error), type="WorkspaceRepoMissing", non_retryable=True) from error
    return WorkspaceResult(workspace_path=str(workspace), skills=skills)
