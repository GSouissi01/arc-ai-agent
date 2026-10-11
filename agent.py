"""
WRFM sample agent - structured-output loop.

Uses OpenAI structured output with provider-specific Pydantic schema variants
over the WRFM API request models. WrfmClient.dispatch() handles routing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import operator
from pathlib import Path
import re
import statistics
import time
from typing import Annotated, List, Literal, Optional, Union

from annotated_types import MaxLen, MinLen
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, Field, ValidationError

from ogchallenge_client import ApiException, CoreClient, TaskInfo, WrfmClient
from ogchallenge_client.dtos import (
    Req_System,
    Req_WikiLoad,
    Req_WikiSearch,
    Req_WikiTree,
    Req_WrfmDataCreate,
    Req_WrfmDataSchema,
    Req_WrfmDataSelect,
    Req_WrfmDataUpdate,
    Req_WrfmRespond,
    WrfmCondition,
    WrfmGroundRef,
)

CLI_GREEN = "\x1b[32m"
CLI_RED = "\x1b[31m"
CLI_CYAN = "\x1b[36m"
CLI_YELLOW = "\x1b[33m"
CLI_BLUE = "\x1b[34m"
CLI_CLR = "\x1b[0m"


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    default_headers: dict[str, str] | None = None


def make_llm_client(config: LLMConfig) -> OpenAI:
    # Higher retry budget for provider 429s (e.g. Groq free tier); the SDK backs off itself.
    kwargs: dict = {"api_key": config.api_key, "max_retries": 6, "timeout": 60.0}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.default_headers:
        kwargs["default_headers"] = config.default_headers
    return OpenAI(**kwargs)


class LocalCalc(BaseModel):
    """Local arithmetic tool, evaluated in code and never sent to ARC."""

    type: Literal["local_calc"]
    expression: str = Field(..., description="Arithmetic only: numbers, + - * / ( ). Example: 2380000 * 14.75 / 24")


class LocalAggregate(BaseModel):
    """Local aggregation over one flat table, computed in code (pages data_select internally)."""

    type: Literal["local_aggregate"]
    table_name: str
    where_clause: List[WrfmCondition] = Field(default_factory=list)
    group_by: Optional[str] = Field(None, description="Field to group by, e.g. EVENT_KEY; null = whole selection")
    value_field: str
    op: Literal["sum", "max", "min", "avg", "count"]


class LocalDowntime(BaseModel):
    """Local tool: downtime hours of [start, end) inside one production day."""

    type: Literal["local_downtime"]
    start: str = Field(..., description="Event start, YYYY-MM-DDTHH:MM:SS")
    end: Optional[str] = Field(None, description="Event end, YYYY-MM-DDTHH:MM:SS; null or empty = ongoing")
    production_day: str = Field(..., description="Production day, YYYY-MM-DD")


class LocalEventCheck(BaseModel):
    """Local tool: rows, missing production days and per-well rate of one deferment event."""

    type: Literal["local_event_check"]
    event_key: str


Action = Union[
    Req_System,
    Req_WikiTree,
    Req_WikiLoad,
    Req_WikiSearch,
    Req_WrfmDataSchema,
    Req_WrfmDataSelect,
    Req_WrfmDataCreate,
    Req_WrfmDataUpdate,
    Req_WrfmRespond,
    LocalCalc,
    LocalAggregate,
    LocalDowntime,
    LocalEventCheck,
]

DiscriminatedAction = Annotated[
    Action,
    Field(discriminator="type"),
]


class NextStep(BaseModel):
    """OpenAI-compatible structured output schema."""

    current_state: str = Field(..., description="Brief summary of what you know so far")
    plan: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ..., description="Remaining steps to complete the task, most important first"
    )
    task_completed: bool = Field(False, description="Set to true only when calling wrfm_respond")
    function: Action = Field(..., description="The next WRFM API call to execute")


class NextStepDiscriminated(BaseModel):
    """Structured output schema with explicit union discriminator metadata."""

    current_state: str = Field(..., description="Brief summary of what you know so far")
    plan: List[str] = Field(
        ..., description="Remaining 3 steps to complete the task, most important first"
    )
    task_completed: bool = Field(False, description="Set to true only when calling wrfm_respond")
    function: DiscriminatedAction = Field(..., description="The next WRFM API call to execute")


def _response_model_for_provider(provider: str) -> type[BaseModel]:
    return NextStep if provider == "openai" else NextStepDiscriminated


def _to_responses_input(log: list[dict]) -> list[dict]:
    items: list[dict] = []
    for msg in log:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in {"system", "developer"}:
            items.append({"role": "developer", "content": content})
        elif role in {"user", "assistant"}:
            items.append({"role": role, "content": content})
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "tool")
            items.append({"role": "user", "content": f"[tool:{tool_call_id}]\n{content}"})
    return items


SYSTEM_PROMPT = """\
You are a WRFM operations agent on NOVA-7, a gas production platform.
You interact with flat deferment and well-production tables through API calls.

Your workflow:
1. Start with system to learn your role and today's date.
2. Read the wiki tree and relevant wiki documents before acting.
3. Read data_schema before using data endpoints.
4. Use data_select to inspect one flat table at a time.
5. Use data_create or data_update only when the task and role authority clearly permit it.
6. Call wrfm_respond with a concise answer, correct outcome, and WRFM ground refs.

Outcome codes:
- ok_answer
- denied_security
- none_clarification_needed
- none_unsupported
- error_internal

Always check necessary governance documents before write actions.
Use deferment guidelines for deferment event and production-day rules.
Use extensive ground refs to reference the exact source of your knowledge and authority, especially for write actions.

All wiki documents are already loaded above. Base every decision on them.

Decision checklist (follow in order):
1. Authority: find your role in [system]. The RACI in governance/raci.md says which role may create, close or update deferment rows. If your role may not do the requested write, respond denied_security and write nothing.
2. Clarify instead of guessing. Respond none_clarification_needed when:
   - several records match and the task does not say which one;
   - a value the write needs is missing and cannot be derived from the data or the wiki (for example, closing an event when no restart or end time is given);
   - the task asks you to set a value through a rule the wiki does not state. DEFER_ACTIVITY_OWNER is the accountable discipline; never infer it from the cause or from your own role.
   Never ask the user for values you can derive yourself: deferred volumes, event key, or event attributes copied from existing rows.
3. Write only when the task asks for a change. A review or check task writes only if it also asks you to correct data, and only where the data breaks a wiki rule.
   For review/correct tasks on an event, run local_event_check: create rows for every missing (well, day) pair and fix rows that break a guide rule. Do not change fields the guide does not require.
   For a group event (G_ key), also create rows for missing_wells reported by local_event_check.
4. Deferment rules:
   - One row per production day per event. Event-level fields are identical across the event's rows.
   - Adding a missing day row for an existing event: copy EVENT_KEY, START_DATE, cause, system group, category, event type, asset type and activity owner from that event's existing rows.
   - New event: build EVENT_KEY exactly as the deferment guide says, matching the format of existing keys. Use approved labels only, choosing the closest approved label for the stated cause and system. Fill every required field. For a new event choose the closest approved labels (cause, system group, activity owner) from the guide. For a row of an existing event copy them.
   - Closing an event: Only when the task states the restart/end time explicitly. Run local_event_check first. Create every missing production-day row WITH END_DATE already set to the closing timestamp; the closing-day row is prorated from 00:00 to END_DATE. Then update END_DATE on all existing rows of the event. Then run local_event_check again and confirm missing is empty and every row has the same END_DATE.
   - Deferred volumes: deferred volume for a row = R × downtime_h / 24, where R is the well's full-day rate. Get downtime_h and uptime_h from local_downtime, and do the arithmetic with local_calc. Choose R in this order:
     a. Existing event (its rows already exist): always use rate_per_well for that well from local_event_check.
     b. New event with no rows yet that is a full stop (EVENT_TYPE DOWN, or type not stated): daily_production ALLOC values are ACTUAL production, already reduced by downtime, so R = ALLOC on a production day with uptime (e.g. the event start day) × 24 / uptime_h for that day.
     c. New event that is a partial curtailment (EVENT_TYPE CONSTRAINT: a rate cut, restriction or limit): the lost rate cannot be derived from the data; respond none_clarification_needed for the volumes instead of guessing.
     Never use a day's ALLOC directly as the rate, and never write 0 or a placeholder volume.
5. Each step executes its function immediately. A create or update that returned a row is done; never repeat it.
6. Data: select one table at a time. Do not use order_by. Use local_aggregate for totals, maximums and per-event sums, and local_calc for every arithmetic step. Never do arithmetic in your head.
7. Tool errors: read the error and change the call. Never repeat an identical failing call. A tool error is never a reason for denied_security or none_unsupported.
8. Ground refs: cite wiki docs by full path, tables by name, wells by WELL_NAME, and deferment events by EVENT_KEY. Cite only items you actually used in this task.
9. Answer format: if the task asks for "only" a value (a figure, a number, a name), the message must be exactly that value and nothing else: no words, no units, no thousands separators, at most 2 decimals, as computed. Otherwise keep messages concise.
"""

MAX_STEPS = 30
FORMAT_RETRIES = 2
FORMAT_RETRY_PROMPT = "Return ONLY the NextStep JSON object matching the schema. Do not call tools. No prose."


class LLMFormatError(RuntimeError):
    """Raised when the LLM keeps returning output that does not match NextStep."""


@dataclass
class _TaskState:
    responded: bool = False
    reason: str = f"Agent reached max steps ({MAX_STEPS}) without responding."
    loaded_docs: set[str] = field(default_factory=set)
    create_keys: set[tuple] = field(default_factory=set)
    recommended: dict[str, set[str]] = field(default_factory=dict)
    role: str = ""
    today: str = ""
    task_text: str = ""
    raci: dict[str, dict[str, str]] = field(default_factory=dict)
    owners: set[tuple[str, str]] = field(default_factory=set)  # (EVENT_KEY, owner lower) seen in data
    required: dict[str, list[str]] = field(default_factory=dict)  # table -> required fields (data_schema)
    enums: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # table -> field -> allowed values
    event_rows: dict[str, list[dict]] = field(default_factory=dict)  # EVENT_KEY -> deferment rows (cache)
    used_tables: set[str] = field(default_factory=set)
    seen_ids: set[str] = field(default_factory=set)  # wells / event keys seen in any result
    trace_path: Path | None = None


def _trace(state, record: dict) -> None:
    """Append one JSON line to this task's trace file; never raises."""
    if state.trace_path is None:
        return
    try:
        with state.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


_ID_KEYS = {"WELL_NAME", "well", "EVENT_KEY", "event_key"}


def _collect_ids(obj, state) -> None:
    """Record every well name / event key appearing anywhere in a result."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _ID_KEYS and isinstance(v, str) and v.strip():
                state.seen_ids.add(v.strip())
            elif k == "wells" and isinstance(v, list):
                state.seen_ids.update(str(w) for w in v)
            else:
                _collect_ids(v, state)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids(v, state)


def _select_all(wrfm: WrfmClient, table: str, where: list | None = None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = wrfm.data_select(table, where_clause=where or [], limit=1000, offset=offset)
        rows.extend(page.rows)
        if page.next_offset == -1 or page.next_offset <= offset:
            return rows
        offset = page.next_offset


def _known_event_rows(key: str | None, state, wrfm: WrfmClient | None) -> list[dict] | None:
    """Existing deferment rows of an event (cached); None if unknown (no client and not seen)."""
    if not key:
        return None
    if key not in state.event_rows and wrfm is not None:
        try:
            state.event_rows[key] = _select_all(
                wrfm, "deferments", [WrfmCondition(field_name="EVENT_KEY", operator="eq", value=key)])
            _track_owners(state.event_rows[key], state)
        except Exception:
            return None
    if key in state.event_rows:
        return state.event_rows[key]
    owners = [o for k, o in state.owners if k == key]
    return [{"EVENT_KEY": key, "DEFER_ACTIVITY_OWNER": o} for o in owners] or None


_EXPLICIT_TIME = [
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"),
    re.compile(r"\b(midday|noon|midnight)\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}\s?(am|pm)\b", re.IGNORECASE),
]


def _has_explicit_time(text: str) -> bool:
    return any(p.search(text or "") for p in _EXPLICIT_TIME)


def _create_key(fn, state):
    keys = state.recommended.get(fn.table_name, set())
    pairs = [(fv.field_name, str(fv.value)) for fv in fn.field_values]
    sig = tuple(sorted(p for p in pairs if p[0] in keys)) or tuple(sorted(pairs))
    return (fn.table_name, sig)


def _parse_raci(markdown: str) -> dict[str, dict[str, str]]:
    """Parse the first markdown table whose header starts with '| Activity |'."""
    lines = [ln.strip() for ln in markdown.splitlines()]
    for i, line in enumerate(lines):
        if not line.lower().startswith("| activity |"):
            continue
        roles = [c.strip().lower() for c in line.strip("|").split("|")][1:]
        table: dict[str, dict[str, str]] = {}
        for row in lines[i + 1:]:
            if not row.startswith("|"):
                break
            cells = [c.strip() for c in row.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):  # separator line
                continue
            table[cells[0].lower()] = {r: v.lower() for r, v in zip(roles, cells[1:])}
        return table
    return {}


def _write_activity(fn, raci: dict[str, dict[str, str]]) -> str | None:
    fields = {fv.field_name for fv in fn.field_values}
    if isinstance(fn, Req_WrfmDataCreate):
        keyword = "create"
    elif "END_DATE" in fields:
        keyword = "close"
    elif "DEFER_ACTIVITY_OWNER" in fields:
        keyword = "activity owner"
    else:
        keyword = "update"
    return next((activity for activity in raci if keyword in activity), None)


def _write_event_key(fn) -> str | None:
    if isinstance(fn, Req_WrfmDataCreate):
        return next((fv.value for fv in fn.field_values if fv.field_name == "EVENT_KEY"), None)
    return next((c.value for c in fn.where_clause if c.field_name == "EVENT_KEY" and c.operator == "eq"), None)


def _track_owners(rows: list[dict], state) -> None:
    for row in rows:
        key, owner = row.get("EVENT_KEY"), row.get("DEFER_ACTIVITY_OWNER")
        if key and owner and str(owner).strip():
            state.owners.add((str(key), str(owner).strip().lower()))


def _write_block_reason(fn, state, wrfm: WrfmClient | None = None):
    if not isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)):
        return None
    if state.raci:
        activity = _write_activity(fn, state.raci)
        if activity:
            value = state.raci[activity].get(state.role.lower(), "")
            if value not in ("accountable", "responsible"):
                return (f"AUTHORITY BLOCKED: per the governance RACI your role '{state.role}' is "
                        f"'{value or 'not listed'}' for '{activity}'. Do not write. Respond denied_security.")
    elif not any("governance" in p.lower() for p in state.loaded_docs):
        return ("WRITE BLOCKED: load the governance (RACI) wiki document and confirm your role "
                "is authorized for this activity before any create or update.")
    values = {fv.field_name: str(fv.value).strip() for fv in fn.field_values}
    is_create = isinstance(fn, Req_WrfmDataCreate)
    key = _write_event_key(fn)
    existing = _known_event_rows(key, state, wrfm) if is_create else None

    if is_create:
        missing = [f for f in state.required.get(fn.table_name, []) if not values.get(f)]
        if missing:
            return (f"MISSING REQUIRED FIELDS: {missing}. For a row of an existing event copy them from that "
                    "event's rows; for a new event use the closest approved label from the deferment guide.")

    owner = values.get("DEFER_ACTIVITY_OWNER", "")
    if owner and not is_create:
        in_task = owner.lower() in state.task_text.lower()
        copied = key is not None and (str(key), owner.lower()) in state.owners
        if not (in_task or copied):
            return ("OWNER BLOCKED: DEFER_ACTIVITY_OWNER is the accountable discipline; it is not stated in the "
                    "task or the event's existing rows and must not be inferred. Leave it unchanged/empty, or "
                    "respond none_clarification_needed if setting it is the task.")
    if owner and is_create:
        if existing:
            expected = sorted({str(r.get("DEFER_ACTIVITY_OWNER") or "").strip() for r in existing} - {""})
            if expected and owner.lower() not in {e.lower() for e in expected}:
                return (f"OWNER BLOCKED: this row belongs to existing event {key}; copy DEFER_ACTIVITY_OWNER "
                        f"from its rows: {expected[0] if len(expected) == 1 else expected}.")
        else:
            allowed = state.enums.get(fn.table_name, {}).get("DEFER_ACTIVITY_OWNER", [])
            if allowed and owner.lower() not in {a.lower() for a in allowed}:
                return (f"OWNER BLOCKED: '{owner}' is not an approved DEFER_ACTIVITY_OWNER label. "
                        f"Allowed values: {allowed}.")

    if values.get("END_DATE") and (not is_create or existing) and not _has_explicit_time(state.task_text):
        return ("END TIME BLOCKED: the task states no restart/end time. Do not assume one. "
                "Respond none_clarification_needed asking for the exact restart timestamp.")

    if is_create:
        volume_block = _volume_check(fn, values, existing, wrfm)
        if volume_block:
            return volume_block

    if is_create and _create_key(fn, state) in state.create_keys:
        return ("DUPLICATE WRITE BLOCKED: this row was already created earlier in this task. "
                "Do not repeat it. Verify with a select, or respond.")
    return None


ORDER_BY_NOTE = ("NOTE: order_by direction is not supported and was dropped; rows are sorted ascending by "
                 "{field}. Use local_aggregate for max/min/sum.")

_CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _calc_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _calc_node(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_node(node.left), _calc_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_calc_node(node.operand)
    raise ValueError(f"unsupported element: {type(node).__name__}")


def _local_calc(expression: str) -> dict:
    """Evaluate arithmetic only (numbers, + - * /, unary minus, parentheses)."""
    try:
        result = _calc_node(ast.parse(expression, mode="eval"))
    except ZeroDivisionError:
        return {"error": "division by zero", "code": "local_calc_error"}
    except (SyntaxError, ValueError) as exc:
        return {"error": f"local_calc accepts arithmetic only: {exc}", "code": "local_calc_error"}
    return {"result": result, "rounded_2dp": round(result, 2)}


def _local_aggregate(wrfm: WrfmClient, fn: LocalAggregate) -> dict:
    """Page through data_select and compute op per group in Python."""
    fields = [fn.group_by, fn.value_field] if fn.group_by else [fn.value_field]
    rows: list[dict] = []
    offset = 0
    try:
        while True:
            page = wrfm.data_select(fn.table_name, fields=fields, where_clause=fn.where_clause,
                                    limit=1000, offset=offset)
            rows.extend(page.rows)
            if page.next_offset == -1 or page.next_offset <= offset:
                break
            offset = page.next_offset
    except ApiException as exc:
        return json.loads(exc.api_error.model_dump_json(exclude_none=True))

    groups: dict[str, dict] = {}
    for row in rows:
        key = str(row.get(fn.group_by)) if fn.group_by else "(all)"
        g = groups.setdefault(key, {"rows": 0, "values": []})
        g["rows"] += 1
        raw = row.get(fn.value_field)
        if raw is None or str(raw).strip() == "":
            continue
        if fn.op == "count":
            g["values"].append(1.0)
            continue
        try:
            g["values"].append(float(raw))
        except (TypeError, ValueError):
            continue

    def compute(values: list[float]) -> float | None:
        if fn.op == "count":
            return float(len(values))
        if not values:
            return None
        if fn.op == "sum":
            return sum(values)
        if fn.op == "max":
            return max(values)
        if fn.op == "min":
            return min(values)
        return sum(values) / len(values)

    result = [{"group": k, "value": compute(g["values"]), "rows": g["rows"]} for k, g in groups.items()]
    result.sort(key=lambda r: (r["value"] is not None, r["value"] or 0.0), reverse=True)
    return {"groups": result[:20], "top": result[0] if result else {}, "total_rows": len(rows)}


def _parse_dt(value) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    return datetime.fromisoformat(str(value).strip())


def _downtime_h(start, end, production_day) -> float:
    """Hours of [start, end) inside [production_day 00:00, +24h); empty end = ongoing."""
    day_start = datetime.fromisoformat(str(production_day).strip()[:10])
    day_end = day_start + timedelta(days=1)
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end) or day_end
    overlap = (min(end_dt, day_end) - max(start_dt, day_start)).total_seconds() / 3600.0
    return round(max(overlap, 0.0), 6)


def _local_downtime(fn: LocalDowntime) -> dict:
    try:
        downtime = _downtime_h(fn.start, fn.end, fn.production_day)
    except (TypeError, ValueError) as exc:
        return {"error": f"local_downtime needs start YYYY-MM-DDTHH:MM:SS and production_day YYYY-MM-DD: {exc}",
                "code": "local_downtime_error"}
    return {"downtime_h": downtime, "uptime_h": round(24.0 - downtime, 6)}


def _float_or_none(value) -> float | None:
    try:
        return None if value is None or str(value).strip() == "" else float(value)
    except ValueError:
        return None


def _rate_per_well(rows: list[dict]) -> dict[str, dict[str, float | None]]:
    """Median full-day rate per well: deferred x 24 / downtime_h over rows with downtime."""
    rates: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        try:
            downtime = _downtime_h(r.get("START_DATE"), r.get("END_DATE"), r.get("PRODUCTION_DAY"))
        except (TypeError, ValueError):
            continue
        if downtime <= 0:
            continue
        w = rates.setdefault(str(r.get("WELL_NAME")), {"gas": [], "cond": []})
        for key, vol_field in (("gas", "DEFERRED_GAS_VOL_SM3"), ("cond", "DEFERRED_COND_VOL_STM3")):
            vol = _float_or_none(r.get(vol_field))
            if vol is not None:
                w[key].append(vol * 24 / downtime)
    return {w: {k: (round(statistics.median(v), 2) if v else None) for k, v in r.items()} for w, r in rates.items()}


def _volume_check(fn, values: dict, existing, wrfm) -> str | None:
    """Validate deferred volumes of a deferment create against R x downtime_h / 24."""
    if fn.table_name != "deferments" or values.get("EVENT_TYPE", "").upper() == "CONSTRAINT":
        return None
    well, day = values.get("WELL_NAME"), values.get("PRODUCTION_DAY")
    try:
        downtime = _downtime_h(values.get("START_DATE"), values.get("END_DATE"), day)
    except (TypeError, ValueError):
        return None
    if downtime <= 0:
        return None
    uptime = 24.0 - downtime
    rate, source = None, None
    if existing and any(r.get("START_DATE") for r in existing):
        per_well = _rate_per_well(existing).get(str(well))
        if per_well and per_well.get("gas") is not None:
            rate, source = per_well, "the event's existing rows (rate_per_well)"
    if rate is None and wrfm is not None and uptime > 0:
        try:
            prod = _select_all(wrfm, "daily_production", [
                WrfmCondition(field_name="WELL_NAME", operator="eq", value=str(well)),
                WrfmCondition(field_name="REPORT_DAY", operator="eq", value=str(day)[:10])])
        except Exception:
            prod = []
        if prod:
            gas, cond = _float_or_none(prod[0].get("ALLOC_GAS_SM3")), _float_or_none(prod[0].get("ALLOC_COND_M3"))
            if gas is not None and cond is not None:
                rate = {"gas": gas * 24 / uptime, "cond": cond * 24 / uptime}
                source = f"daily_production ALLOC on {str(day)[:10]} x 24 / uptime_h {round(uptime, 4)}"
    if rate is None:
        return None
    expected = {"gas": round(rate["gas"] * downtime / 24, 2),
                "cond": round((rate.get("cond") or 0.0) * downtime / 24, 2)}
    for key, vol_field in (("gas", "DEFERRED_GAS_VOL_SM3"), ("cond", "DEFERRED_COND_VOL_STM3")):
        given = _float_or_none(values.get(vol_field))
        if given is None or given == 0 or abs(given - expected[key]) > 0.01 * abs(expected[key]):
            return (f"VOLUME CHECK: expected DEFERRED_GAS_VOL_SM3={expected['gas']} and "
                    f"DEFERRED_COND_VOL_STM3={expected['cond']} (rate R={round(rate['gas'], 2)} from {source}, "
                    f"downtime_h={round(downtime, 4)}). Resubmit with these values.")
    return None


def _local_event_check(wrfm: WrfmClient, fn: LocalEventCheck, state) -> dict:
    """All rows of one event: missing (well, day) pairs, rows without END_DATE, per-well rate."""
    rows: list[dict] = []
    offset = 0
    where = [WrfmCondition(field_name="EVENT_KEY", operator="eq", value=fn.event_key)]
    try:
        while True:
            page = wrfm.data_select("deferments", where_clause=where, limit=1000, offset=offset)
            rows.extend(page.rows)
            if page.next_offset == -1 or page.next_offset <= offset:
                break
            offset = page.next_offset
    except ApiException as exc:
        return json.loads(exc.api_error.model_dump_json(exclude_none=True))
    if not rows:
        return {"error": f"no deferments rows with EVENT_KEY {fn.event_key!r}", "code": "event_not_found"}
    _track_owners(rows, state)

    starts = sorted({str(r.get("START_DATE") or "") for r in rows} - {""})
    ends = sorted({str(r.get("END_DATE") or "") for r in rows} - {""})
    wells = sorted({str(r.get("WELL_NAME")) for r in rows})
    first_day = _parse_dt(starts[0]).date()
    if ends:
        end_dt = _parse_dt(ends[-1])
        last_day = end_dt.date() - timedelta(days=1) if end_dt.time() == datetime.min.time() else end_dt.date()
    else:
        # Ongoing: expected through yesterday; today's production day is not complete yet.
        today = date.fromisoformat(state.today[:10]) if state.today else max(
            date.fromisoformat(str(r["PRODUCTION_DAY"])[:10]) for r in rows) + timedelta(days=1)
        last_day = today - timedelta(days=1)

    present = {(str(r.get("WELL_NAME")), str(r.get("PRODUCTION_DAY"))[:10]) for r in rows}
    missing = []
    d = first_day
    while d <= last_day:
        missing += [{"well": w, "day": d.isoformat()} for w in wells if (w, d.isoformat()) not in present]
        d += timedelta(days=1)

    detail = []
    for r in rows:
        downtime = _downtime_h(r.get("START_DATE"), r.get("END_DATE"), r.get("PRODUCTION_DAY"))
        gas, cond = _float_or_none(r.get("DEFERRED_GAS_VOL_SM3")), _float_or_none(r.get("DEFERRED_COND_VOL_STM3"))
        detail.append({"well": r.get("WELL_NAME"), "day": str(r.get("PRODUCTION_DAY"))[:10], "downtime_h": downtime,
                       "gas": gas, "cond": cond, "END_DATE": r.get("END_DATE") or ""})
    detail.sort(key=lambda x: (x["well"] or "", x["day"]))

    result = {
        "event_key": fn.event_key,
        "start": starts[0] if len(starts) == 1 else starts,
        "end": (ends[0] if len(ends) == 1 else ends) if ends else "",
        "wells": wells,
        "rows": detail,
        "missing": missing,
        "rate_per_well": _rate_per_well(rows),
    }
    if ends:
        result["rows_without_end_date"] = [{"well": x["well"], "day": x["day"]} for x in detail if not x["END_DATE"]]
    if fn.event_key.startswith("G_"):
        result["missing_wells"] = _group_missing_wells(wrfm, set(wells), first_day, last_day)
        for mw in result["missing_wells"]:
            missing += [{"well": mw["well"], "day": d} for d in mw["days"]]
    return result


def _group_missing_wells(wrfm: WrfmClient, event_wells: set[str], first_day: date, last_day: date) -> list[dict]:
    """Wells without rows in a group event that were producing and not closed or deferred in its window."""
    days = []
    d = first_day
    while d <= last_day:
        days.append(d.isoformat())
        d += timedelta(days=1)
    all_wells = sorted({str(r.get("WELL_NAME")) for r in _select_all(wrfm, "well_info")} - {"None", ""})
    found = []
    for well in all_wells:
        if well in event_wells:
            continue
        cond = [WrfmCondition(field_name="WELL_NAME", operator="eq", value=well)]
        status = []
        for s in _select_all(wrfm, "well_status_history", cond):
            s_start = str(s.get("STATUS_DATE") or "")[:10]
            s_end = str(s.get("STATUS_END_DATE") or "")[:10]
            if s_start and s_start <= days[-1] and (not s_end or s_end >= days[0]):
                status.append(s)
        closed_cover = any(str(s.get("STATUS_DESC", "")).upper().startswith("CLOSED")
                           and str(s.get("STATUS_DATE"))[:10] <= days[0]
                           and (not str(s.get("STATUS_END_DATE") or "").strip()
                                or str(s.get("STATUS_END_DATE"))[:10] >= days[-1]) for s in status)
        other = [r for r in _select_all(wrfm, "deferments", cond) if str(r.get("PRODUCTION_DAY"))[:10] in days]
        alloc = {str(p.get("REPORT_DAY"))[:10]: _float_or_none(p.get("ALLOC_GAS_SM3"))
                 for p in _select_all(wrfm, "daily_production", cond) if str(p.get("REPORT_DAY"))[:10] in days}
        producing = any((v or 0) > 0 for v in alloc.values())
        if not closed_cover and not other and producing:
            found.append({
                "well": well,
                "days": days,
                "evidence": {
                    "status_intervals_in_window": [{k: s.get(k) for k in ("STATUS_DESC", "STATUS_DATE", "STATUS_END_DATE")}
                                                   for s in status],
                    "other_deferment_rows_in_window": len(other),
                    "alloc_gas_on_event_days": alloc,
                },
            })
    return found


def _enrich_refs(fn: Req_WrfmRespond, state) -> tuple[Req_WrfmRespond, list[dict]]:
    """Drop refs never seen in this task; add a table ref for every table used. Never adds wiki refs."""
    task_lower = state.task_text.lower()
    kept, dropped = [], []
    for ref in fn.ground_refs:
        rid = str(ref.id)
        if ref.type == "wiki":
            ok = rid in state.loaded_docs
        elif ref.type == "table":
            ok = rid in state.used_tables
        elif ref.type in ("well", "deferment_event"):
            ok = rid in state.seen_ids or rid.lower() in task_lower
        else:
            ok = True
        (kept if ok else dropped).append(ref)
    cited_tables = {str(r.id) for r in kept if r.type == "table"}
    for table in sorted(state.used_tables - cited_tables):
        kept.append(WrfmGroundRef(type="table", id=table, label=table))
    return fn.model_copy(update={"ground_refs": kept}), [r.model_dump(exclude_none=True) for r in dropped]


TREE_CHARS = "│├└─|`+-– \t"


def _tree_paths(tree: str) -> list[str]:
    """Best-effort parse of a wiki_tree string into full paths."""
    paths: list[str] = []
    stack: list[str] = []
    entries = []
    for line in tree.splitlines():
        stripped = line.lstrip(TREE_CHARS)
        name = stripped.strip()
        if name and name not in {".", "./"}:  # skip blank lines and the root marker
            entries.append((len(line) - len(stripped), name))
    base = min((indent for indent, _ in entries), default=0)
    for indent, name in entries:
        if "/" in name.rstrip("/"):  # already a full path
            paths.append(name.rstrip("/"))
            continue
        depth = (indent - base) // 4
        stack = stack[:depth]
        full = "/".join(stack + [name.rstrip("/")])
        paths.append(full)
        if name.endswith("/") or "." not in name:
            stack.append(name.rstrip("/"))
    return paths


def _call_llm(
    client: OpenAI,
    llm_config: LLMConfig,
    log: list[dict],
    response_model: type[BaseModel],
    *,
    spec_id: str,
    step_num: int,
):
    """Call the LLM and return (resp, parsed NextStep), retrying format failures.

    Retries use a temporary copy of the log with a format reminder; failed
    attempts are never added to the conversation history.
    """
    messages = log
    code = "unknown"
    for attempt in range(FORMAT_RETRIES + 1):
        try:
            if llm_config.provider == "openai":
                resp = client.responses.parse(
                    model=llm_config.model,
                    input=_to_responses_input(messages),
                    text_format=response_model,
                    temperature=0,
                )
                step = resp.output_parsed
            else:
                resp = client.beta.chat.completions.parse(
                    model=llm_config.model,
                    response_format=response_model,
                    messages=messages,
                    temperature=0,
                )
                step = resp.choices[0].message.parsed
            if step is not None:
                return resp, step
            code = "parse_none"
        except BadRequestError as exc:
            if getattr(exc, "code", None) != "tool_use_failed" and "tool_use_failed" not in str(exc):
                raise
            code = "tool_use_failed"
        except ValidationError:
            code = "validation_error"

        if attempt == FORMAT_RETRIES:
            break
        print(
            f"\n    {CLI_YELLOW}RETRY spec={spec_id} step={step_num} code={code} "
            f"attempt={attempt + 1}/{FORMAT_RETRIES}{CLI_CLR} ",
            end="",
            flush=True,
        )
        messages = log + [{"role": "user", "content": FORMAT_RETRY_PROMPT}]

    raise LLMFormatError(
        f"LLM output did not match NextStep after {FORMAT_RETRIES} retries (last error: {code})"
    )


def _send_fallback_respond(wrfm: WrfmClient, reason: str) -> None:
    print(f"\n  {CLI_YELLOW}Sending fallback respond: error_internal - {reason}{CLI_CLR}")
    try:
        wrfm.respond(message=reason, outcome="error_internal")
    except Exception as exc:
        print(f"  {CLI_RED}Fallback respond failed: {exc}{CLI_CLR}")


def run_agent(
    api: CoreClient,
    task: TaskInfo,
    *,
    llm_config: LLMConfig,
) -> None:
    """Run one task; guarantees exactly one recorded wrfm_respond per task."""
    wrfm = api.get_wrfm_client(task)
    state = _TaskState()
    try:
        _run_agent_loop(api, task, wrfm, llm_config, state)
    except BaseException as exc:
        state.reason = f"Agent failed: {type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        if not state.responded:
            _send_fallback_respond(wrfm, state.reason)
            _trace(state, {"final_respond": {"message": state.reason, "outcome": "error_internal"}, "fallback": True})


def _run_agent_loop(
    api: CoreClient,
    task: TaskInfo,
    wrfm: WrfmClient,
    llm_config: LLMConfig,
    state: _TaskState,
) -> None:
    client = make_llm_client(llm_config)

    print(f"\n{CLI_CYAN}Task: {task.spec_id}{CLI_CLR}")
    print(f"  {task.task_text}\n")

    try:
        trace_dir = Path(__file__).resolve().parent / "runs" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        state.trace_path = trace_dir / f"{task.spec_id}_{stamp}.jsonl"
    except Exception:
        state.trace_path = None
    _trace(state, {"task": task.spec_id, "task_text": task.task_text})

    bootstrap_log = _bootstrap(wrfm, state)
    _trace(state, {"bootstrap": [{"label": label, "text": text} for label, text in bootstrap_log]})

    log: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    for label, text in bootstrap_log:
        print(f"  {CLI_GREEN}AUTO {label}{CLI_CLR}: {text[:120]}")
        log.append({"role": "user", "content": f"[{label}]\n{text}"})
        if label == "data_schema":
            try:
                tables = json.loads(text)["tables"]
                state.recommended = {
                    table: set(info.get("recommended_filters", [])) for table, info in tables.items()
                }
                for table, info in tables.items():
                    fields = info.get("fields") or {}
                    state.required[table] = [f for f, spec in fields.items() if spec.get("required")]
                    state.enums[table] = {f: spec["enum"] for f, spec in fields.items() if spec.get("enum")}
            except Exception:
                state.recommended = {}
        elif label == "system":
            try:
                info = json.loads(text)
                state.role, state.today = str(info.get("role", "")), str(info.get("today", ""))
            except Exception:
                pass
        elif label.startswith("wiki:") and not state.raci:
            state.raci = _parse_raci(text)

    state.task_text = task.task_text
    log.append({"role": "user", "content": task.task_text})
    response_model = _response_model_for_provider(llm_config.provider)

    for i in range(MAX_STEPS):
        step_id = f"step_{i + 1}"
        print(f"  Step {i + 1}... ", end="", flush=True)

        t0 = time.time()
        try:
            resp, step = _call_llm(
                client, llm_config, log, response_model,
                spec_id=task.spec_id, step_num=i + 1,
            )
        except LLMFormatError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "LLM request failed for "
                f"provider={llm_config.provider!r}, model={llm_config.model!r}. "
                "Check MODEL_PROVIDER, MODEL_ID, and the matching provider API key. "
                f"Original error: {exc}"
            ) from exc
        elapsed_ms = int((time.time() - t0) * 1000)

        if llm_config.provider == "openai":
            prompt_tokens = resp.usage.input_tokens if resp.usage else None
            completion_tokens = resp.usage.output_tokens if resp.usage else None
            cached_prompt_tokens = getattr(getattr(resp.usage, "input_tokens_details", None), "cached_tokens", None)
        else:
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else None
            completion_tokens = resp.usage.completion_tokens if resp.usage else None
            cached_prompt_tokens = getattr(getattr(resp.usage, "prompt_tokens_details", None), "cached_tokens", None)

        fn = step.function
        fn_type = fn.type
        fn_args = fn.model_dump_json(exclude_none=True, exclude={"type"})
        print(f"{CLI_CYAN}{fn_type}{CLI_CLR} - {step.plan[0]}  ({elapsed_ms}ms)")
        print(f"    {CLI_YELLOW}args:{CLI_CLR} {fn_args[:300]}")

        try:
            api.log_llm(
                task_id=task.task_id,
                completion=step.plan[0],
                model=llm_config.model,
                duration_sec=(time.time() - t0),
                prompt_tokens=prompt_tokens,
                cached_prompt_tokens=cached_prompt_tokens if isinstance(cached_prompt_tokens, int) else None,
                completion_tokens=completion_tokens,
            )
        except Exception:
            pass

        log.append({
            "role": "assistant",
            "content": f"STATE: {step.current_state}\nEXECUTED: {fn_type} {fn.model_dump_json(exclude_none=True)}",
            "tool_calls": [{
                "type": "function",
                "id": step_id,
                "function": {
                    "name": type(fn).__name__,
                    "arguments": fn.model_dump_json(exclude_none=True),
                },
            }],
        })

        dispatch_ok = False
        block = _write_block_reason(fn, state, wrfm)
        dropped_refs = []
        if isinstance(fn, Req_WrfmRespond):
            fn, dropped_refs = _enrich_refs(fn, state)
        if isinstance(fn, (LocalCalc, LocalAggregate, LocalDowntime, LocalEventCheck)):
            try:
                if isinstance(fn, LocalCalc):
                    local = _local_calc(fn.expression)
                elif isinstance(fn, LocalAggregate):
                    local = _local_aggregate(wrfm, fn)
                elif isinstance(fn, LocalDowntime):
                    local = _local_downtime(fn)
                else:
                    local = _local_event_check(wrfm, fn, state)
            except Exception as exc:
                local = {"error": str(exc), "code": "local_tool_error"}
            if isinstance(fn, LocalAggregate) and "error" not in local:
                state.used_tables.add(fn.table_name)
            if isinstance(fn, LocalEventCheck) and "error" not in local:
                state.used_tables.add("deferments")
            _collect_ids(local, state)
            result_text = json.dumps(local)
            style = CLI_RED if "error" in local else CLI_GREEN
            print(f"    {style}->{CLI_CLR} {result_text[:200]}")
        elif block:
            result_text = json.dumps({"error": block, "code": "blocked_by_agent_guard"})
            print(f"    {CLI_RED}BLOCKED: {block}{CLI_CLR}")
        else:
            note = ""
            to_send = fn
            if isinstance(fn, Req_WrfmDataSelect) and fn.order_by and len(fn.order_by.split()) > 1:
                order_field = fn.order_by.split()[0]  # bare field name only; direction is not supported
                to_send = fn.model_copy(update={"order_by": order_field})
                note = ORDER_BY_NOTE.format(field=order_field) + "\n"
            try:
                result = wrfm.dispatch(to_send)
                result_text = result.model_dump_json(exclude_none=True)
                dispatch_ok = True
                print(f"    {CLI_GREEN}->{CLI_CLR} {result_text[:200]}")
                if isinstance(fn, Req_WikiLoad):
                    state.loaded_docs.add(fn.path)
                if isinstance(fn, Req_WrfmDataSelect):
                    _track_owners(getattr(result, "rows", []) or [], state)
                if isinstance(fn, Req_WrfmDataCreate):
                    state.create_keys.add(_create_key(fn, state))
                if isinstance(fn, (Req_WrfmDataSelect, Req_WrfmDataCreate, Req_WrfmDataUpdate)):
                    state.used_tables.add(fn.table_name)
                    _collect_ids(getattr(result, "rows", []) or [], state)
                if isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)):
                    state.event_rows.clear()  # rows changed; refetch on next guard check
            except ApiException as exc:
                result_text = exc.api_error.model_dump_json(exclude_none=True)
                print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
            except Exception as exc:
                result_text = f'{{"error": "{exc}"}}'
                print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
            result_text = note + result_text

        log.append({"role": "tool", "content": f"RESULT of {fn_type}: {result_text}", "tool_call_id": step_id})
        _trace(state, {
            "step": i + 1, "current_state": step.current_state, "plan": step.plan, "function": fn_type,
            "args": json.loads(fn.model_dump_json(exclude_none=True)), "result": result_text,
            "block": block if not isinstance(fn, (LocalCalc, LocalAggregate, LocalDowntime, LocalEventCheck)) else None,
            "dropped_refs": dropped_refs, "elapsed_ms": elapsed_ms,
            "tokens": {"prompt": prompt_tokens, "cached": cached_prompt_tokens, "completion": completion_tokens},
        })

        if isinstance(fn, Req_WrfmRespond):
            _trace(state, {"final_respond": json.loads(fn.model_dump_json(exclude_none=True)), "dispatch_ok": dispatch_ok})
            if dispatch_ok:
                state.responded = True
            else:
                state.reason = f"Agent respond was rejected by the API: {result_text}"[:300]
            print(f"\n  {CLI_GREEN}Agent responded: {fn.outcome}{CLI_CLR}")
            print(f"  {CLI_BLUE}{fn.message}{CLI_CLR}")
            if fn.ground_refs:
                for ref in fn.ground_refs:
                    print(f"    ref: {ref.type} -> {ref.id}")
            break
    else:
        print(f"\n  {CLI_YELLOW}Reached max steps ({MAX_STEPS}) without responding.{CLI_CLR}")


def _bootstrap(wrfm: WrfmClient, state: _TaskState) -> list[tuple[str, str]]:
    results = []
    wiki_paths: list[str] = []

    try:
        system = wrfm.system()
        results.append(("system", system.model_dump_json()))
    except Exception as exc:
        results.append(("system", f"error: {exc}"))

    try:
        wiki = wrfm.wiki_tree()
        results.append(("wiki_tree", wiki.tree))
        wiki_paths = [p for p in _tree_paths(wiki.tree) if p.endswith(".md")]
    except Exception as exc:
        results.append(("wiki_tree", f"error: {exc}"))

    try:
        schema = wrfm.data_schema()
        results.append(("data_schema", schema.model_dump_json()))
    except Exception as exc:
        results.append(("data_schema", f"error: {exc}"))

    # Load the whole wiki up front (pages are small) so every decision can cite it.
    for path in wiki_paths:
        try:
            page = wrfm.dispatch(Req_WikiLoad(path=path))
            results.append((f"wiki:{path}", page.content))
            state.loaded_docs.add(path)
        except Exception as exc:
            results.append((f"wiki:{path}", f"error: {exc}"))

    return results
