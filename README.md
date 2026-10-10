# ARC WRFM Sample Agent

Starter template for the **ARC wrfm-ops** benchmark.

This agent uses OpenAI structured output to interact with flat WRFM tables for
deferment and well-production tasks on the NOVA-7 gas production platform.

## Architecture

```
main.py   - session/task orchestrator
agent.py  - WRFM agent loop, prompt, structured output schema, bootstrap
```

The agent uses a structured-output loop:

1. Bootstrap - auto-runs `system`, `wiki_tree`, and `data_schema`.
2. Loop - LLM returns a `NextStep` Pydantic model on each iteration.
3. Dispatch - `WrfmClient.dispatch()` routes the request to the API.
4. Done - loop exits when the agent calls `wrfm_respond`.

## Quick Start

```bash
# From arc-ogchallenge repo root, install the local SDK in editable mode.
pip install -e .

cd examples/sample-agent-wrfm
uv sync
```

Configure `.env` like the maintenance sample agent:

```bash
ARC_BASE_URL=http://localhost:8000
ARC_API_KEY=...
MODEL_PROVIDER=openai
OPENAI_API_KEY=...
MODEL_ID=gpt-4.1-2025-04-14
```

Run all WRFM tasks in a session:

```bash
uv run python main.py
```

Run one WRFM task by exact spec id:

```bash
uv run python main.py --spec wrfm_def_001
make task SPEC=wrfm_def_001
```

## Notes

- `wrfm-ops` may be hidden from public benchmark metadata. For single-task
  development this runner creates a WRFM session and selects the exact `spec_id`
  from `session_status`.
- Ground refs for WRFM are limited to `wiki`, `table`, `well`, and `deferment_event`.
- Use `deferment_event` with `id` equal to the `EVENT_KEY` value from `deferments`.
- The WRFM API is flat-table only in V1: no joins, group-by, views, or SQL parser.
