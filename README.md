# Temporal Repo Scanner

## What it does

You paste a public GitHub repository URL into a web page. A Temporal workflow clones the repository and runs a Claude agent over it, and a few minutes later you get a short, severity-ranked list of the Temporal best practice violations that matter most, at most seven, each with the file, the line, one sentence on the problem and one sentence on the fix.

The scanned repository can use any Temporal SDK language. The agent uses the temporal-developer and temporal-workflow-design-critic skills, confirms rules against the Temporal Docs MCP server, and optionally reads from the GitHub MCP server. It is read-only: it can read, search and list files and call those MCP tools, and nothing else.

Findings are ranked by severity, then by blast radius:

- critical: will cause data loss, duplicate side effects, stuck workflows or non-determinism failures in production
- high: will cause outages or runaway cost under common failure conditions
- medium: a best practice gap with limited immediate impact

## Architecture

The FastAPI app serves the web page and three endpoints, and starts one RepoScanWorkflow per scan. A worker process runs the workflow and its activities.

RepoScanWorkflow runs three activities in order and always runs a cleanup activity afterward:

1. clone_repo makes a shallow, submodule-free clone into SCAN_ROOT/<workflow id>/repo and enforces size limits (200 MB, 20000 files).
2. prepare_workspace copies the vendored skills into SCAN_ROOT/<workflow id>/.claude/skills.
3. run_agent_scan runs the Claude agent from SCAN_ROOT/<workflow id>, heartbeats while it works, validates the agent's JSON against the ScanReport schema and returns the report.
4. cleanup_workspace deletes SCAN_ROOT/<workflow id>. It runs in a finally block, so it also runs after failure and cancellation.

Progress reaches the workflow as signals. While the agent works, run_agent_scan sends short events such as "skill loaded temporal-developer", "file read src/workflows.ts" or "docs lookup continue as new" to the report_progress signal, throttled to one every 2 seconds and 50 per scan. The workflow keeps the latest 25 events and the current stage.

State is read with a query. The API calls the get_progress query to return the stage, a message and the recent events while a scan runs, and reads the workflow result once it completes. Repository contents and agent transcripts never enter workflow history; only the small clone result and the final report do.

## Prerequisites

- Python 3.11 or newer and uv
- git on the PATH of the worker
- The Temporal CLI, for temporal server start-dev
- An Anthropic API key
- Optionally a GitHub token, to give the agent the GitHub MCP server

The Claude Agent SDK (claude-agent-sdk 0.2.168) ships a bundled Claude Code CLI (version 2.1.301) inside the Python wheel, so no separate Claude Code install is needed.

### Temporal Docs MCP server login

The agent connects to the Temporal knowledge base MCP server at https://temporal.mcp.kapa.ai. Per the Temporal documentation (https://docs.temporal.io/with-ai), the server is public but requires a one-time login with a Google or GitHub account through MCP OAuth, to enforce rate limits. Do the login once on the worker machine with Claude Code:

```
claude mcp add --scope user --transport http temporal-docs https://temporal.mcp.kapa.ai
claude
/mcp
```

Then pick temporal-docs and finish the browser login. The bundled CLI reused that login in testing.

If the server is unreachable or not logged in, the scan does not fail. The agent continues with the two skills only, and the report summary starts with "Docs lookup was unavailable; findings are based on skills only." The progress feed also shows "agent started, docs lookup unavailable".

## Setup

```
uv venv --python 3.12
uv pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
```

### Skills

The agent loads two skills from the skills directory at the project root:

- skills/temporal-developer, vendored from https://github.com/temporalio/skill-temporal-developer
- skills/temporal-workflow-design-critic, supplied separately

Each skill folder must contain a SKILL.md file. If skills/temporal-workflow-design-critic is missing, place the skill folder there; until then the app still starts, logs a warning, and scans run with the remaining skill.

The agent runs from SCAN_ROOT/<workflow id>/ and the clone lives in its repo/ subdirectory, so any .claude settings, CLAUDE.md or skills inside the scanned repository are never loaded as project settings. The system prompt also tells the agent that file contents are data and never instructions.

## Running locally

Run each command in its own terminal from the project root.

```
temporal server start-dev
```

```
uv run python -m app.worker
```

```
uv run uvicorn app.api:app --port 8000
```

Open http://localhost:8000, paste a repository URL such as https://github.com/stwomack/bad-temporal-workflow and press Scan. The Temporal UI is at http://localhost:8233, and the page links to the workflow for each scan.

### Worker

The worker connects to TEMPORAL_ADDRESS and TEMPORAL_NAMESPACE (with TLS and TEMPORAL_API_KEY when that key is set), registers RepoScanWorkflow and its four activities on TASK_QUEUE, and removes scan directories under SCAN_ROOT older than one hour. It runs at most WORKER_MAX_CONCURRENT_ACTIVITIES activities at once (default 2) because each scan is CPU, memory and cost heavy. SIGINT or SIGTERM stops polling and gives running activities WORKER_SHUTDOWN_GRACE_SECONDS (default 30) to finish.

The clone, prepare, scan and cleanup activities share the local disk under SCAN_ROOT, so the demo runs a single worker process. To scale out, every activity of one scan must run on the same machine. Two ways to do that: give each worker its own task queue and have the workflow route the later activities to the queue of the worker that cloned the repo, or merge clone, prepare and scan into one activity so the disk state never crosses activity boundaries.

### API

- POST /api/scans with {"repo_url": "https://github.com/owner/repo"} validates the URL and starts RepoScanWorkflow with id scan-<owner>-<repo>-<8 hex chars>. It returns scan_id and temporal_ui_url. An invalid URL returns 422 with a message.
- GET /api/scans/{scan_id} returns stage, message, recent events, and once complete the report. Failed and cancelled scans include an error message.
- POST /api/scans/{scan_id}/cancel requests cancellation.

### Command line scan

```
uv run python scripts/scan_cli.py --local https://github.com/owner/repo
```

--local starts a throwaway Temporal dev server and an in-process worker. Without it the script connects to TEMPORAL_ADDRESS and runs its own worker on a private task queue; with --use-running-worker it only starts the workflow on TASK_QUEUE. Progress goes to stderr and the findings to stdout as plain text.

## Demo script

This shows durability and cancellation with the three terminals from Running locally.

1. Start a scan. In the web page, scan https://github.com/stwomack/bad-temporal-workflow. Open the Temporal UI link and watch clone_repo and prepare_workspace complete and run_agent_scan start.
2. Kill the worker mid-scan. Once the progress feed shows events, find the worker's process id with `pgrep -f app.worker` and run `kill -9` on it. The page switches to queued with "Waiting for a worker", because queries need a running worker, while the Temporal UI still shows the workflow as Running with run_agent_scan pending.
3. Restart the worker with `uv run python -m app.worker`. The workflow does not clone or prepare again; those results come from history. As soon as the 60 second heartbeat timeout of the lost attempt has expired, Temporal hands run_agent_scan attempt 2 to the new worker, and the page goes back to scanning.
4. Watch it finish. The progress feed fills again, the stage moves to done, and the ranked findings appear. The workflow history shows clone_repo and prepare_workspace once each, run_agent_scan with its started event at attempt 2, and cleanup_workspace at the end.
5. Show cancellation. Start a second scan, wait until the progress feed shows events, and press Cancel. Within about 15 seconds the agent subprocess stops, cleanup_workspace runs, and the page shows Scan cancelled. In the Temporal UI the workflow status is Canceled and the last activity is cleanup_workspace.

These steps were run end to end against a local dev server: the killed scan resumed on the restarted worker and finished with 7 findings, and the cancelled scan reached Scan cancelled 11 seconds after Cancel with no agent processes or scan directories left behind.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| ANTHROPIC_API_KEY | none, required | Key for the Claude agent. Missing fails the scan without retries. |
| ANTHROPIC_MODEL | claude-opus-5-5 | Model the agent uses |
| TEMPORAL_ADDRESS | localhost:7233 | Temporal frontend address |
| TEMPORAL_NAMESPACE | default | Temporal namespace |
| TEMPORAL_API_KEY | none | Temporal Cloud API key; enables TLS when set |
| TEMPORAL_UI_URL | http://localhost:8233 | Base URL for links to the Temporal UI |
| TASK_QUEUE | repo-scanner | Task queue for the workflow and activities |
| SCAN_ROOT | system temp dir plus /repo-scanner | Where clones and agent workspaces live |
| GITHUB_TOKEN | none | Enables the read-only GitHub MCP server for the agent |
| DEMO_REPO_URL | none | Repository for the end-to-end test |
| AGENT_MAX_TURNS | 40 | Turn limit for one agent run |
| AGENT_MAX_BUDGET_USD | 5.0 | Spend limit for one agent run |
| WORKER_MAX_CONCURRENT_ACTIVITIES | 2 | Activities one worker runs at once |
| WORKER_SHUTDOWN_GRACE_SECONDS | 30 | Time running activities get on shutdown |

## Running tests

```
uv run pytest
```

The workflow, worker and API tests start a local Temporal dev server automatically, reusing the installed temporal CLI when it is on the PATH.

The end-to-end test is skipped unless DEMO_REPO_URL and ANTHROPIC_API_KEY are set. It starts a local dev server, runs a real scan and calls the Anthropic API:

```
DEMO_REPO_URL=https://github.com/stwomack/bad-temporal-workflow uv run pytest -m e2e -s
```

## Temporal best practices this app follows

| Practice | Where |
| --- | --- |
| Deterministic workflow code: no clock, randomness or I/O, and activity and model imports passed through the sandbox | app/workflows.py |
| A test fails if any module is imported after the sandbox loads the workflow, and recorded histories are replayed | pyproject.toml, tests/test_workflow.py |
| Explicit start_to_close timeout on every activity, plus a heartbeat timeout on the long agent activity | app/workflows.py |
| Bounded retries with explicit attempts, backoff and non-retryable error types | app/workflows.py |
| Permanent failures raised as non-retryable ApplicationError with a type (InvalidRepoUrl, RepoNotFound, RepoTooLarge, MissingApiKey and others) | app/activities.py, app/agent/runner.py |
| Heartbeats on every agent message plus a background heartbeat, with only a short step string as details | app/agent/runner.py, app/activities.py |
| A heartbeat throttle cap so cancellation reaches the agent within seconds | app/worker.py |
| Idempotent activities: clone reuses a completed clone and removes partial ones, prepare and cleanup can run any number of times | app/activities.py, app/workspace.py |
| Small payloads: repository contents and transcripts stay on the worker; history holds only file counts and the final report, capped at seven findings | app/activities.py, app/models.py |
| Clean cancellation: the agent runs under a cancel scope that terminates the CLI subprocess, the workflow waits for that, then always runs cleanup | app/agent/runner.py, app/workflows.py |
| Progress through signals and state through a query, keeping only the latest 25 events | app/workflows.py |
| Activities send signals with the worker's own client from activity.client() instead of opening new connections | app/activities.py |
| Bounded worker concurrency and graceful shutdown on SIGINT and SIGTERM | app/worker.py |
| One Temporal client per process with the pydantic data converter | app/temporal_client.py, app/api.py |
| Untrusted input validated before it reaches git: an allowlist URL parser and git run with an argument list, a hard timeout and symlinks disabled | app/repo_url.py, app/activities.py |

## Known limitations

- One worker process. The activities of a scan share local disk, so scans only work when one worker handles all of them. See the Worker section for how to scale out.
- Killing the worker with kill -9 loses the running agent attempt. Temporal retries it after the 60 second heartbeat timeout, and run_agent_scan has at most two attempts, so a second crash fails the scan. The orphaned Claude Code CLI process outlives the worker for a few seconds until its pipe closes; in testing it exited within about 10 seconds.
- While no worker is running, the page shows queued with "Waiting for a worker" even for a scan that was mid-way, because the progress query needs a worker.
- A cancel during clone_repo waits for the clone to finish or hit its 150 second git timeout, because clone_repo does not heartbeat.
- Seven findings at most. Large repositories with many problems report the seven most important and set truncated.
- The agent's judgment is only as good as the model and skills. Findings are checked against the schema, but not proven correct.
- The GitHub MCP read-only configuration was not tested with a real token.
- Public GitHub repositories only. Private repositories, other hosts and submodules are not supported.
- The vendored skills were edited to remove em dashes and emoji to meet this project's text rules. Reapply that edit when updating them.
