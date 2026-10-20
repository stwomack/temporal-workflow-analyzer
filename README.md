# Temporal Repo Scanner

Submit a public GitHub repository URL and get back a short, severity-ranked list of Temporal best practice violations, at most seven, so you know what to fix first.

## Setup

Requires Python 3.11 or newer and uv.

```
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Configuration

All configuration comes from environment variables: ANTHROPIC_API_KEY, ANTHROPIC_MODEL, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_API_KEY, TASK_QUEUE, SCAN_ROOT, GITHUB_TOKEN and DEMO_REPO_URL. See app/config.py for defaults.

## Running tests

```
uv run pytest
```
