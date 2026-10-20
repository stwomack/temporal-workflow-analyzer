# Temporal Repo Scanner

Submit a public GitHub repository URL and get back a short, severity-ranked list of Temporal best practice violations, at most seven, so you know what to fix first.

## Setup

Requires Python 3.11 or newer, uv, and git on the PATH of the worker.

The Claude Agent SDK (claude-agent-sdk 0.2.168) ships a bundled Claude Code CLI (version 2.1.301) inside the Python wheel, so no separate Claude Code install is needed. Set cli_path only if you want to use a different CLI.

```
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Skills

The agent loads two skills from the skills directory at the project root:

- skills/temporal-developer, vendored from https://github.com/temporalio/skill-temporal-developer
- skills/temporal-workflow-design-critic, supplied separately

Each skill folder must contain a SKILL.md file. If skills/temporal-workflow-design-critic is missing, place the skill folder there; until then the app still starts, logs a warning, and scans run with the remaining skill.

For every scan the worker copies these skills fresh into SCAN_ROOT/<workflow id>/.claude/skills/ and clones the repository into SCAN_ROOT/<workflow id>/repo/. The agent runs from SCAN_ROOT/<workflow id>/, so any .claude settings inside the scanned repository are never loaded as project settings.

## Worker

Start a worker with:

```
uv run python -m app.worker
```

The worker connects to TEMPORAL_ADDRESS and TEMPORAL_NAMESPACE (with TLS and TEMPORAL_API_KEY when that key is set), registers RepoScanWorkflow and its four activities on TASK_QUEUE, and removes scan directories under SCAN_ROOT that are older than one hour. It runs at most WORKER_MAX_CONCURRENT_ACTIVITIES activities at once (default 2) because each scan is CPU, memory and cost heavy. SIGINT or SIGTERM stops polling and gives running activities WORKER_SHUTDOWN_GRACE_SECONDS (default 30) to finish.

The clone, prepare, scan and cleanup activities share the local disk under SCAN_ROOT, so the demo runs a single worker process. To scale out, every activity of one scan must run on the same machine. Two ways to do that: give each worker its own task queue and have the workflow route the later activities to the queue of the worker that cloned the repo, or merge clone, prepare and scan into one activity so the disk state never crosses activity boundaries.

## Configuration

All configuration comes from environment variables: ANTHROPIC_API_KEY, ANTHROPIC_MODEL, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_API_KEY, TASK_QUEUE, SCAN_ROOT, GITHUB_TOKEN, DEMO_REPO_URL, AGENT_MAX_TURNS (default 40), AGENT_MAX_BUDGET_USD (default 5.0), WORKER_MAX_CONCURRENT_ACTIVITIES (default 2) and WORKER_SHUTDOWN_GRACE_SECONDS (default 30). See app/config.py for defaults.

## Running tests

```
uv run pytest
```
