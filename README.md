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

## Configuration

All configuration comes from environment variables: ANTHROPIC_API_KEY, ANTHROPIC_MODEL, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_API_KEY, TASK_QUEUE, SCAN_ROOT, GITHUB_TOKEN, DEMO_REPO_URL, AGENT_MAX_TURNS (default 40) and AGENT_MAX_BUDGET_USD (default 5.0). See app/config.py for defaults.

## Running tests

```
uv run pytest
```
