# ARC WRFM Agent

Agent for the **ARC wrfm-ops** benchmark ([agentreliabilitychallenge.com](https://agentreliabilitychallenge.com)).

It handles deferment and well-production tasks on the NOVA-7 gas production
platform through the flat WRFM tables. An LLM drives a structured-output loop;
deterministic code computes the numbers and guards every write and response.

## Layout

```
main.py                      session/task orchestrator, CLI, config + preflight checks
agent.py                     agent loop, system prompt, NextStep schema, local tools, guards
docs/prompt-changes.md       log of every SYSTEM_PROMPT change (P-001, P-002, ...)
tools/dump_wiki.py           read-only: dump wiki pages to runs/wiki/
tools/check_volume_rule.py   read-only: check the deferred-volume rule against stored rows
tests/test_fix_*.py          offline regression tests, one per fix
```

## How the agent works

1. **Bootstrap**: runs `system`, `wiki_tree`, `data_schema` and loads the wiki
   documents into the prompt.
2. **Loop** (max 30 steps): the LLM returns a `NextStep` (state, plan, next
   function). Format errors get up to 2 retries.
3. **Dispatch**: WRFM API calls go through `WrfmClient.dispatch()`; local tools
   run in code and never reach ARC.
4. **Done**: the loop ends on `wrfm_respond`. If the loop fails, a fallback
   response is sent.

### Local tools

| Tool | Purpose |
|---|---|
| `local_calc` | Safe arithmetic (`+ - * /`, parentheses) |
| `local_aggregate` | sum / max / min / avg / count over one table, optional group-by (pages `data_select`) |
| `local_downtime` | Downtime hours of `[start, end)` inside one production day |
| `local_volume_estimate` | Deferred gas/condensate volume for one well and production day |
| `local_event_check` | Rows, missing production days and per-well rate of one deferment event |

### Guards (in code, not just in the prompt)

- **Authority**: writes are checked against the RACI in `governance/raci.md`;
  disallowed writes are blocked.
- **Clarification**: writes are blocked when a required value is missing, e.g.
  closing an event without a stated end time.
- **Event consistency**: event-level fields must match the event's existing
  rows; new `W_` events take their asset type from the well's own precedent.
- **Volumes**: `round(rate * seconds / 86400, 2)`, checked before every write.
- **Responses**: ground refs are enriched and checked; answer-only numbers are
  formatted (thousands separators, 2 dp for volumes).

Each fix is tagged `fix-NNN` in the git history and has a matching test in `tests/`.

## Setup

```bash
uv sync
```

Create `.env` (never committed):

```bash
ARC_API_KEY=...            # or ARC_AUTH_TOKEN
MODEL_PROVIDER=openai      # or openrouter
OPENAI_API_KEY=...         # or OPENROUTER_API_KEY
MODEL_ID=gpt-4.1-mini
# optional: ARC_BASE_URL (defaults to https://agentreliabilitychallenge.com)
```

`main.py` checks the platform and LLM config before any task starts.

## Running

Dev run of one task by exact spec id (workspace `dev-wrfm` by default):

```bash
uv run python main.py --spec wrfm_def_001
```

Scored session (runs **all** tasks and submits):

```bash
uv run python main.py --submit --workspace <name>
```

Without `--spec` or `--submit`, `main.py` refuses to start and exits with code 2,
so a bare `python main.py` (or `make run`) never starts a scored session.

Traces are written to `runs/traces/` (git-ignored, like all of `runs/`).

## Tests

The tests are offline: they use snapshots in `runs/data/` and `runs/wiki/`
(made by the `tools/` scripts) and make no network or LLM calls. Pass the repo
root as the first argument:

```bash
uv run python tests/test_fix_014.py .
```

## WRFM API notes

- Flat tables only: no joins, group-by, views or SQL. Aggregates go through `local_aggregate`.
- `order_by` direction is not supported; rows come back sorted ascending.
- Ground refs: `wiki`, `table`, `well`, `deferment_event` (id = `EVENT_KEY`).
