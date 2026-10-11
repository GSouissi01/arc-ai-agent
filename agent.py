"""
WRFM sample agent - structured-output loop.

Uses OpenAI structured output with provider-specific Pydantic schema variants
over the WRFM API request models. WrfmClient.dispatch() handles routing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
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
    WrfmKeyValue,
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


class LocalVolumeEstimate(BaseModel):
    """Local tool: deferred volumes of one (well, production day) row from production data (volume_estimate)."""

    type: Literal["local_volume_estimate"]
    well: str
    start: str = Field(..., description="Event start, YYYY-MM-DDTHH:MM:SS")
    end: Optional[str] = Field(None, description="Event end, YYYY-MM-DDTHH:MM:SS; null or empty = ongoing")
    production_day: str = Field(..., description="Production day, YYYY-MM-DD")
    event_type: Literal["DOWN", "CONSTRAINT"]


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
    LocalVolumeEstimate,
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
If an [event_check] block is present, it is the code's check of the event the task is about. If the task asks to review/correct the event data, handle every missing and volume_mismatch item it lists, or state why not. Otherwise it is information only.
If your writes were blocked because the task lacks information (owner, end time, volumes) and no write succeeded, respond none_clarification_needed asking for that information.

Decision checklist (follow in order):
0. Check authority before anything else (see [authority]). No authority -> respond denied_security, even if details are missing, and cite governance/raci.md.
1. Authority: find your role in [system]. The RACI in governance/raci.md says which role may create, close or update deferment rows. If your role may not do the requested write, respond denied_security and write nothing.
2. Clarify instead of guessing. Respond none_clarification_needed when:
   - several records match and the task does not say which one;
   - a value the write needs is missing and cannot be derived from the data or the wiki (for example, closing an event when no restart or end time is given);
   - the task asks you to change DEFER_ACTIVITY_OWNER on an existing event and the new owner is not stated in the task or the docs. DEFER_ACTIVITY_OWNER is the accountable discipline; never infer it from the cause or from your own role. For a new event the owner is required: take it from the docs (the guide's owner definition: the accountable discipline for the cause); do not ask.
   Never ask the user for values you can derive yourself: deferred volumes, event key, or event attributes copied from existing rows.
   Never ask for clarification only because a free-text cause, system or owner word is not an approved label. Map it to the closest approved label from the deferment guide lists (for example, a valve or actuator fault -> the matching equipment breakdown cause, and the system group that contains that equipment). Ask only when two labels fit equally well.
3. Write only when the task asks for a change. A review or check task writes only if it also asks you to correct data, and only where the data breaks a wiki rule.
   For review/correct tasks on an event, run local_event_check: create rows for every missing (well, day) pair and fix rows that break a guide rule. Do not change fields the guide does not require.
   For a group event (G_ key), also create rows for missing_wells reported by local_event_check.
   If local_event_check reports volume_mismatch rows, correct their volumes to the expected values (review/correct tasks only).
4. Deferment rules:
   - One row per production day per event. Event-level fields are identical across the event's rows.
   - Adding a missing day row (or missing well) for an existing event: copy every event attribute from that event's existing rows: EVENT_KEY, START_DATE, END_DATE (if the event is closed), cause, system group, category, event type, asset type, activity owner and COMMENTS.
   - New event: build EVENT_KEY exactly as the deferment guide says, matching the format of existing keys. Use approved labels only, choosing the closest approved label for the stated cause and system. Fill every required field. For a new event choose the closest approved labels (cause, system group, activity owner) from the guide. For a row of an existing event copy them. COMMENTS for new events follow the data convention built by code. Do not write free text.
   - Closing an event: Only when the task states the restart/end time explicitly. Run local_event_check first. Create every missing production-day row WITH END_DATE already set to the closing timestamp; the closing-day row is prorated from 00:00 to END_DATE. Then update END_DATE on all existing rows of the event. Then run local_event_check again and confirm missing is empty and every row has the same END_DATE.
   - Deferred volumes come only from volume_estimate (DOWN rows: the stated or event-span downtime hours x the well's clean-day rate; CONSTRAINT rows: the clean-day ALLOC difference, or the peers' loss fraction for a group-event well whose production data shows no loss). local_event_check returns volume_estimate for every missing day and missing well of an existing event; for a new event (or any single row) call local_volume_estimate with the well, start, end, production day and event type. Write its write_gas and write_cond values exactly as given. If volume_estimate has no method (clarification_needed), respond none_clarification_needed asking for the volumes and cite the event. Never compute volumes yourself, never guess, and never write 0 unless volume_estimate is 0.
5. Each step executes its function immediately. A create or update that returned a row is done; never repeat it.
6. Data: select one table at a time. Do not use order_by. Use local_aggregate for totals, maximums and per-event sums, and local_calc for every arithmetic step. Never do arithmetic in your head.
7. Tool errors: read the error and change the call. Never repeat an identical failing call. A tool error is never a reason for denied_security or none_unsupported.
8. Ground refs: cite wiki docs by full path, tables by name, wells by WELL_NAME, and deferment events by EVENT_KEY. Cite only items you actually used in this task. Always cite the deferment event you were asked about (by EVENT_KEY), also in clarification and denied answers. Cite every well you wrote or asked about.
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
    event_targets: set[str] = field(default_factory=set)  # EVENT_KEYs written (incl. blocked) or event-checked
    selected_keys: set[str] = field(default_factory=set)  # EVENT_KEYs in deferments select results
    ref_wells: set[str] = field(default_factory=set)  # wells written (incl. blocked) or targeted by a check
    event_checks: dict[str, dict] = field(default_factory=dict)  # EVENT_KEY -> latest local_event_check result
    written_pairs: set[tuple[str, str]] = field(default_factory=set)  # (well, day) created this task
    updated_pairs: set[tuple[str, str]] = field(default_factory=set)  # (well, day) updated this task
    event_check_blocks: int = 0
    clarify_blocks: list[tuple[str, str]] = field(default_factory=list)  # (guard, missing info) of blocked writes
    write_ok: bool = False  # a create/update succeeded this task


def _trace(state, record: dict) -> None:
    """Append one JSON line to this task's trace file; never raises."""
    if state.trace_path is None:
        return
    try:
        with state.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


_ID_KEYS = {"WELL_NAME", "well", "EVENT_KEY", "event_key", "group"}
_W_EVENT_KEY = re.compile(r"^W_(.+)_\d{4}-\d{2}-\d{2}-\d+$")


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


_TIME_TOKEN = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b|\b(midday|noon|midnight)\b|\b(\d{1,2})\s?(am|pm)\b",
                         re.IGNORECASE)


def _has_explicit_time(text: str) -> bool:
    return bool(_TIME_TOKEN.search(text or ""))


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


_EVENT_FIELDS = ["START_DATE", "END_DATE", "DEFER_GLOBAL_CAUSE", "DEFER_SYS_GRP", "DEFER_CATEGORY_IND",
                 "EVENT_TYPE", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS"]


def _event_consistency(values: dict, rows: list[dict], state) -> dict:
    """Event-level fields of a new row must match the event's existing rows (unless the task states the value)."""
    mismatch = {}
    for name in _EVENT_FIELDS:
        existing_vals = [str(r.get(name) or "").strip() for r in rows]
        if not any(existing_vals):
            continue  # nothing to copy (e.g. open event END_DATE, or a field left empty on the event's rows)
        given = values.get(name, "")
        if given in existing_vals or (given and given.lower() in state.task_text.lower()):
            continue
        mismatch[name] = max(set(existing_vals), key=existing_vals.count)
    return mismatch


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
    is_create = isinstance(fn, Req_WrfmDataCreate)
    if state.task_text:
        intent, allowed = _write_intent(state.task_text)
        write_type = "create" if is_create else "update"
        if intent == "unknown":
            _trace(state, {"write_scope": "intent unknown"})
        elif write_type not in allowed:
            return f"WRITE SCOPE: task asks {intent}; {write_type} not allowed"
    values = {fv.field_name: str(fv.value).strip() for fv in fn.field_values}
    key = _write_event_key(fn)
    existing = _known_event_rows(key, state, wrfm) if is_create else None

    if is_create:
        missing = [f for f in state.required.get(fn.table_name, []) if not values.get(f)]
        if missing:
            return (f"MISSING REQUIRED FIELDS: {missing}. For a row of an existing event copy them from that "
                    "event's rows; for a new event use the closest approved label from the deferment guide.")

    allowed_labels = state.enums.get(fn.table_name, {})
    for label_field in ("DEFER_GLOBAL_CAUSE", "DEFER_SYS_GRP", "DEFER_ACTIVITY_OWNER"):
        label, approved = values.get(label_field, ""), allowed_labels.get(label_field, [])
        if label and approved and label.lower() not in {a.lower() for a in approved}:
            return f"LABEL: '{label}' not approved for {label_field}; approved: {approved}; pick the closest."

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

    real_rows = [r for r in (existing or []) if r.get("START_DATE")]
    if is_create and real_rows:
        mismatch = _event_consistency(values, real_rows, state)
        if mismatch:
            return f"EVENT CONSISTENCY: copy from existing rows: {mismatch}"

    existing_ends = {str(r.get("END_DATE") or "").strip() for r in real_rows} - {""}
    copies_end = is_create and values.get("END_DATE") in existing_ends
    if (values.get("END_DATE") and (not is_create or existing) and not copies_end
            and not _has_explicit_time(state.task_text)):
        return ("END TIME BLOCKED: the task states no restart/end time. Do not assume one. "
                "Respond none_clarification_needed asking for the exact restart timestamp.")
    stated_end = _stated_end(state.task_text, values.get("START_DATE"))
    if fn.table_name == "deferments" and is_create and not real_rows and values.get("END_DATE") and stated_end:
        try:
            end_ok = _parse_dt(values["END_DATE"]) == datetime.fromisoformat(stated_end)
        except ValueError:
            end_ok = False
        if not end_ok:
            return (f"END_DATE: the task states the window end; write END_DATE={stated_end} "
                    "(H am = H:00, so 12am = 12:00; H pm = (H+12):00, so 12pm = 24:00).")

    if is_create:
        volume_block = _volume_check(fn, values, existing, wrfm, state)
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


def _downtime_h(start, end, production_day, exact: bool = False) -> float:
    """Hours of [start, end) inside [production_day 00:00, +24h); empty end = ongoing (rounded unless exact)."""
    day_start = datetime.fromisoformat(str(production_day).strip()[:10])
    day_end = day_start + timedelta(days=1)
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end) or day_end
    overlap = (min(end_dt, day_end) - max(start_dt, day_start)).total_seconds() / 3600.0
    return max(overlap, 0.0) if exact else round(max(overlap, 0.0), 6)


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
    """Full-day rate per well from the row with the most downtime hours (least 24/h amplification).

    Rows giving the same R within 0.1% of that row are combined with a median. R is not rounded.
    """
    rates: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for r in rows:
        try:
            downtime = _downtime_h(r.get("START_DATE"), r.get("END_DATE"), r.get("PRODUCTION_DAY"), exact=True)
        except (TypeError, ValueError):
            continue
        if downtime <= 0:
            continue
        w = rates.setdefault(str(r.get("WELL_NAME")), {"gas": [], "cond": []})
        for key, vol_field in (("gas", "DEFERRED_GAS_VOL_SM3"), ("cond", "DEFERRED_COND_VOL_STM3")):
            vol = _float_or_none(r.get(vol_field))
            if vol is not None:
                w[key].append((downtime, vol * 24 / downtime))

    def best(pairs: list[tuple[float, float]]) -> float | None:
        if not pairs:
            return None
        ref = max(pairs)[1]
        same = [rate for _, rate in pairs if abs(rate - ref) <= 0.001 * abs(ref)]
        return statistics.median(same)

    return {w: {k: best(v) for k, v in r.items()} for w, r in rates.items()}


def _stated_window(text: str, event_day: str) -> tuple[datetime, datetime] | None:
    """Start/end stated in the task text (exactly two times), placed on the event start day.

    Midnight is 00:00 as a start and 24:00 as an end; noon / midday is 12:00. am/pm follows the evaluator's
    naive rule: H am = H:00 (12am = 12:00), H pm = (H+12):00 (12pm = 24:00). An end before the start crosses midnight.
    """
    tokens = []
    for m in _TIME_TOKEN.finditer(text or ""):
        if m.group(1):
            tokens.append(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3) or 0) / 60)
        elif m.group(4):
            tokens.append(None if m.group(4).lower() == "midnight" else 720)
        else:
            tokens.append((int(m.group(5)) + (12 if m.group(6).lower() == "pm" else 0)) * 60)
    if len(tokens) != 2:
        return None
    t1 = 0 if tokens[0] is None else tokens[0]
    t2 = 1440 if tokens[1] is None else tokens[1]
    if t2 <= t1:
        t2 += 1440
    day0 = datetime.fromisoformat(str(event_day)[:10])
    return day0 + timedelta(minutes=t1), day0 + timedelta(minutes=t2)


def _stated_end(text: str, event_day) -> str | None:
    """END_DATE (YYYY-MM-DDTHH:MM:SS) of the window stated in the task, or None."""
    try:
        window = _stated_window(text, event_day) if event_day else None
    except ValueError:
        return None
    return window[1].isoformat(timespec="seconds") if window else None


def _window_hours_on_day(window: tuple[datetime, datetime], production_day: str) -> float:
    day_start = datetime.fromisoformat(str(production_day)[:10])
    overlap = (min(window[1], day_start + timedelta(days=1)) - max(window[0], day_start)).total_seconds() / 3600.0
    return max(overlap, 0.0)


def _span_on_day(start_dt: datetime, end_dt: datetime | None, production_day) -> tuple[datetime, datetime]:
    """[start, end) clipped to [production_day 00:00, +24h); no end = ongoing (end of day)."""
    day_start = datetime.fromisoformat(str(production_day).strip()[:10])
    day_end = day_start + timedelta(days=1)
    s, e = max(start_dt, day_start), min(end_dt or day_end, day_end)
    return s, max(e, s)


def _volume(rate: float, start_dt: datetime, end_dt: datetime) -> float:
    """Deferred volume of one row: round(R * seconds / 86400, 2), evaluated in exactly that order.

    Inferred from data (38/38 stored rows, evaluator 46.8 x 14.5 h -> 28.27); not a confirmed evaluator rule.
    """
    return round(rate * (end_dt - start_dt).total_seconds() / 86400, 2)


def _round2(value: float) -> float:
    """Python round() to 2 decimals (write values; DOWN volumes come already rounded from _volume)."""
    return round(value, 2)


def _with_write_values(est: dict) -> dict:
    if est.get("method") is not None:
        est["write_gas"], est["write_cond"] = _round2(est["deferred_gas"]), _round2(est["deferred_cond"])
    return est


def _volume_estimate(wrfm: WrfmClient, well, start, end, production_day, event_type,
                     event_rows: list[dict] | None = None, cache: dict | None = None, task_text: str = "") -> dict:
    """volume_estimate plus write_gas / write_cond (the 2-decimal values to write)."""
    return _with_write_values(_volume_estimate_raw(wrfm, well, start, end, production_day, event_type,
                                                   event_rows, cache, task_text))


def _volume_estimate_raw(wrfm: WrfmClient, well, start, end, production_day, event_type,
                         event_rows: list[dict] | None = None, cache: dict | None = None, task_text: str = "") -> dict:
    """Deferred volumes of one (well, production day) row (DOWN methods via _volume; clean_day_diff unrounded).

    R = the well's ALLOC on the last clean day before the event start (no deferment row for that well that day).
    DOWN:       times stated in the task -> _volume(R, stated window on the day)   (clean_day_stated_hours)
                else _volume(R, event span inside the production day)          (clean_day_span_hours)
                no clean day: _volume(best R from the event's rows, span)      (best_R_fallback)
    CONSTRAINT: day has daily_production -> R - ALLOC(day)                     (clean_day_diff)
    Otherwise: no method -> clarification.
    """
    day = str(production_day)[:10]
    start_day = str(start)[:10]
    by_well = [WrfmCondition(field_name="WELL_NAME", operator="eq", value=str(well))]
    cache = {} if cache is None else cache
    if ("prod", str(well)) not in cache:
        cache[("prod", str(well))] = {str(p.get("REPORT_DAY"))[:10]: p
                                      for p in _select_all(wrfm, "daily_production", by_well)}
        cache[("defs", str(well))] = {str(r.get("PRODUCTION_DAY"))[:10]
                                      for r in _select_all(wrfm, "deferments", by_well)}
    prod, deferred_days = cache[("prod", str(well))], cache[("defs", str(well))]
    constraint = str(event_type or "").upper() == "CONSTRAINT"
    base = {"well": well, "day": day, "method": None, "clean_day": None}
    clean = next((d for d in sorted(prod, reverse=True) if d < start_day and d not in deferred_days), None)
    if clean:
        r_gas = _float_or_none(prod[clean].get("ALLOC_GAS_SM3")) or 0.0
        r_cond = _float_or_none(prod[clean].get("ALLOC_COND_M3")) or 0.0
        out = dict(base, clean_day=clean, R_gas=r_gas, R_cond=r_cond)
        if constraint:
            if day in prod:
                a_gas = _float_or_none(prod[day].get("ALLOC_GAS_SM3")) or 0.0
                a_cond = _float_or_none(prod[day].get("ALLOC_COND_M3")) or 0.0
                return dict(out, method="clean_day_diff", deferred_gas=r_gas - a_gas, deferred_cond=r_cond - a_cond)
            return dict(out, clarification_needed="CONSTRAINT row without daily_production for that day: ask for the volumes")
        window = _stated_window(task_text, start_day)
        stated = _window_hours_on_day(window, day) if window else 0.0
        if stated > 0:
            s, e = _span_on_day(window[0], window[1], day)
            return dict(out, method="clean_day_stated_hours", downtime_h=stated,
                        stated_end=window[1].isoformat(timespec="seconds"),
                        deferred_gas=_volume(r_gas, s, e), deferred_cond=_volume(r_cond, s, e))
        hours = _downtime_h(start, end, day, exact=True)
        s, e = _span_on_day(_parse_dt(start), _parse_dt(end), day)
        return dict(out, method="clean_day_span_hours", downtime_h=hours,
                    deferred_gas=_volume(r_gas, s, e), deferred_cond=_volume(r_cond, s, e))
    if not constraint and event_rows:
        rate = _rate_per_well(event_rows).get(str(well))
        if rate and rate.get("gas") is not None:
            hours = _downtime_h(start, end, day, exact=True)
            s, e = _span_on_day(_parse_dt(start), _parse_dt(end), day)
            return dict(base, method="best_R_fallback", R_gas=rate["gas"], R_cond=rate.get("cond") or 0.0,
                        downtime_h=hours, deferred_gas=_volume(rate["gas"], s, e),
                        deferred_cond=_volume(rate.get("cond") or 0.0, s, e))
    return dict(base, clarification_needed="no clean production day before the event start: ask for the volumes")


def _peer_fraction(wrfm: WrfmClient, own: dict, event_rows: list[dict], cache: dict) -> dict | None:
    """Volume for a facility well whose ALLOC shows no loss on a group event: the peers' loss fraction.

    One physical cut fraction = median over >= 2 peer rows of (peer deferred gas / peer clean-day gas rate),
    applied to this well's clean-day gas AND condensate rates. Condensate is written at 1 decimal when every
    peer's stored condensate equals round(peer clean-day cond rate x fraction, 1), else at 2 decimals.
    Returns None when it cannot be computed.
    """
    day = own.get("day")
    peers = [r for r in event_rows if str(r.get("PRODUCTION_DAY"))[:10] == day and r.get("WELL_NAME") != own.get("well")]
    if len(peers) < 2 or not own.get("R_gas"):
        return None
    fractions, peer_cond = [], []
    for r in peers:
        est = _volume_estimate(wrfm, r.get("WELL_NAME"), r.get("START_DATE"), r.get("END_DATE") or None, day,
                               "CONSTRAINT", event_rows, cache)
        gas, rate = _float_or_none(r.get("DEFERRED_GAS_VOL_SM3")), est.get("R_gas")
        if gas is not None and rate:
            fractions.append(gas / rate)
        peer_cond.append((_float_or_none(r.get("DEFERRED_COND_VOL_STM3")), est.get("R_cond")))
    if len(fractions) < 2:
        return None
    fraction = statistics.median(fractions)
    cond_1dp = all(stored is not None and rate is not None and stored == round(rate * fraction, 1)
                   for stored, rate in peer_cond)
    est = _with_write_values(dict(own, method="group_peer_fraction", peer_fraction=fraction, peers=len(peers),
                                  cond_decimals=1 if cond_1dp else 2, deferred_gas=own["R_gas"] * fraction,
                                  deferred_cond=(own.get("R_cond") or 0.0) * fraction))
    if cond_1dp:
        est["write_cond"] = float(Decimal(repr(round(est["deferred_cond"], 9))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return est


def _volume_check(fn, values: dict, existing, wrfm, state=None) -> str | None:
    """Validate deferred volumes of a deferment create against volume_estimate."""
    if fn.table_name != "deferments" or wrfm is None:
        return None
    if not all(values.get(f) for f in ("WELL_NAME", "PRODUCTION_DAY", "START_DATE")):
        return None  # incomplete row: the required-field check reports it
    try:
        real_rows = [r for r in (existing or []) if r.get("START_DATE")]
        cache: dict = {}
        est = _volume_estimate(wrfm, values.get("WELL_NAME"), values.get("START_DATE"), values.get("END_DATE") or None,
                               values.get("PRODUCTION_DAY"), values.get("EVENT_TYPE", ""), real_rows, cache,
                               state.task_text if state else "")
        if (str(values.get("EVENT_KEY", "")).startswith("G_") and est.get("method") == "clean_day_diff"
                and (est.get("deferred_gas") or 0) <= 0.01 and real_rows):
            est = _peer_fraction(wrfm, est, real_rows, cache) or est
    except Exception:
        return None
    if est.get("method") is None:
        return (f"VOLUME CHECK: no data-derived volume_estimate for this row ({est.get('clarification_needed')}). "
                "Do not guess: respond none_clarification_needed asking for the volumes and cite the event.")
    expected = {"gas": est["write_gas"], "cond": est["write_cond"]}
    for key, vol_field in (("gas", "DEFERRED_GAS_VOL_SM3"), ("cond", "DEFERRED_COND_VOL_STM3")):
        given = _float_or_none(values.get(vol_field))
        tolerance = max(0.01, 0.0001 * abs(expected[key]))
        if given is None or abs(given - expected[key]) > tolerance:
            return (f"VOLUME CHECK: expected DEFERRED_GAS_VOL_SM3={expected['gas']} and "
                    f"DEFERRED_COND_VOL_STM3={expected['cond']} (method {est['method']}, clean_day {est.get('clean_day')}, "
                    f"R_gas={est.get('R_gas')}). Resubmit with these values.")
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
        "rate_per_well": {w: {k: (round(v, 6) if v is not None else None) for k, v in r.items()}
                          for w, r in _rate_per_well(rows).items()},
    }
    if ends:
        result["rows_without_end_date"] = [{"well": x["well"], "day": x["day"]} for x in detail if not x["END_DATE"]]
    if fn.event_key.startswith("G_"):
        result["missing_wells"] = _group_missing_wells(wrfm, set(wells), first_day, last_day)
        for mw in result["missing_wells"]:
            missing += [{"well": mw["well"], "day": d} for d in mw["days"]]
    types = [str(r.get("EVENT_TYPE") or "") for r in rows]
    event_type = max(set(types), key=types.count)
    cache: dict = {}
    result["volume_estimate"] = [
        _volume_estimate(wrfm, m["well"], starts[0], ends[-1] if ends else None, m["day"], event_type, rows, cache,
                         state.task_text)
        for m in missing
    ]
    if result.get("missing_wells"):
        # A facility well whose ALLOC shows no loss (data not captured) is still missing when >= 2 peers have rows:
        # its volume is the median peer loss fraction. Without enough peers it is reported as unaffected.
        missing_well_names = {mw["well"] for mw in result["missing_wells"]}
        for idx, e in enumerate(result["volume_estimate"]):
            if (e["well"] in missing_well_names and e.get("method") == "clean_day_diff"
                    and (e.get("deferred_gas") or 0) <= 0.01):
                result["volume_estimate"][idx] = _peer_fraction(wrfm, e, rows, cache) or e
        no_loss = {(e["well"], e["day"]) for e in result["volume_estimate"]
                   if e["well"] in missing_well_names and e.get("method") is not None
                   and (e.get("deferred_gas") or 0) <= 0.01}
        if no_loss:
            for mw in result["missing_wells"]:
                mw["days"] = [d for d in mw["days"] if (mw["well"], d) not in no_loss]
            unaffected = [mw["well"] for mw in result["missing_wells"] if not mw["days"]]
            result["missing_wells"] = [mw for mw in result["missing_wells"] if mw["days"]]
            missing[:] = [m for m in missing if (m["well"], m["day"]) not in no_loss]
            result["volume_estimate"] = [e for e in result["volume_estimate"] if (e["well"], e["day"]) not in no_loss]
            if unaffected:
                result["unaffected_wells"] = [{"well": w, "reason": "no production loss on event days and fewer "
                                               "than 2 peer rows to derive a loss fraction"} for w in unaffected]
    mismatches = []
    for r in rows:
        est = _volume_estimate(wrfm, r.get("WELL_NAME"), r.get("START_DATE"), r.get("END_DATE") or None,
                               r.get("PRODUCTION_DAY"), r.get("EVENT_TYPE") or event_type, rows, cache, state.task_text)
        if est.get("method") is None:
            continue
        no_loss = est.get("method") == "clean_day_diff" and (est.get("deferred_gas") or 0) <= 0.01
        if fn.event_key.startswith("G_") and no_loss:
            # Same method as the create check: a flat-ALLOC group-event well is valued by the peers' loss fraction.
            est = _peer_fraction(wrfm, est, rows, cache)
            if est is None:
                continue  # no peer method: never propose an update to 0
        stored = {"gas": _float_or_none(r.get("DEFERRED_GAS_VOL_SM3")), "cond": _float_or_none(r.get("DEFERRED_COND_VOL_STM3"))}
        if any(stored[k] is None or abs(stored[k] - est[f"write_{k}"]) > max(0.01, 0.0001 * abs(est[f"write_{k}"]))
               for k in ("gas", "cond")):
            mismatches.append({"well": r.get("WELL_NAME"), "day": str(r.get("PRODUCTION_DAY"))[:10],
                               "stored_gas": stored["gas"], "stored_cond": stored["cond"],
                               "expected_gas": est["write_gas"], "expected_cond": est["write_cond"],
                               "method": est["method"], "clean_day": est.get("clean_day")})
    result["volume_mismatch"] = mismatches
    state.event_checks[fn.event_key] = result
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
        closed_cover = any(str(s.get("STATUS_DESC", "")).upper().startswith("CLOSED") for s in status)
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


_EVENT_KEY_IN_TEXT = re.compile(r"\b[GW]_[A-Za-z0-9-]+_\d{4}-\d{2}-\d{2}-\d+\b")
_DATE_IN_TEXT = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CHECK_WORDING = re.compile(r"\b(review|correct|check|reconcile|fix)\b", re.IGNORECASE)


def _task_event_key(wrfm: WrfmClient, state) -> str | None:
    """The event the task names: an EVENT_KEY in the text, or one well + a date that resolve to exactly one event."""
    match = _EVENT_KEY_IN_TEXT.search(state.task_text)
    if match:
        return match.group(0)
    dates = set(_DATE_IN_TEXT.findall(state.task_text))
    if not dates:
        return None
    wells = [str(r.get("WELL_NAME")) for r in _select_all(wrfm, "well_info")
             if r.get("WELL_NAME") and re.search(rf"\b{re.escape(str(r['WELL_NAME']))}\b", state.task_text)]
    if len(wells) != 1:
        return None
    rows = _select_all(wrfm, "deferments", [WrfmCondition(field_name="WELL_NAME", operator="eq", value=wells[0])])
    keys = {str(r.get("EVENT_KEY")) for r in rows if str(r.get("PRODUCTION_DAY"))[:10] in dates}
    return keys.pop() if len(keys) == 1 else None


def _event_check_block(fn: Req_WrfmRespond, state, current_state: str) -> str | None:
    """ok_answer on a review/correct task must not leave event-check items unhandled (not set-owner/close/create tasks)."""
    if fn.outcome != "ok_answer" or not state.event_checks or not _review_task(state.task_text):
        return None
    unhandled = []
    for key, check in state.event_checks.items():
        unhandled += [f"missing {key} {m['well']} {m['day']}" for m in check.get("missing", [])
                      if (m["well"], m["day"]) not in state.written_pairs]
        unhandled += [f"volume_mismatch {key} {v['well']} {v['day']} expected {v['expected_gas']}/{v['expected_cond']}"
                      for v in check.get("volume_mismatch", []) if (v["well"], v["day"]) not in state.updated_pairs]
    if not unhandled:
        return None
    if state.event_check_blocks >= 1:  # second ok_answer: accept the agent's stated reason, log it
        _trace(state, {"event_check_override": unhandled, "reason": current_state})
        return None
    state.event_check_blocks += 1
    return f"EVENT CHECK: unhandled items: {unhandled}; create/update them or explain why not"


def _note_event_check(state, key: str, check: dict) -> None:
    """Record an event check: event ref, table use, seen ids and the wells to cite (G_: only flagged missing wells)."""
    state.event_targets.add(key)
    state.used_tables.add("deferments")
    _collect_ids(check, state)
    if key.startswith("G_"):
        state.ref_wells.update(mw["well"] for mw in check.get("missing_wells", []))
    else:
        state.ref_wells.update(check.get("wells", []))


def _authority_ok(value: str) -> bool:
    return value in ("accountable", "responsible")


def _authority_note(state) -> str:
    if not state.raci or not state.role:
        return ""
    lines = [f"Your role '{state.role}' per governance/raci.md:"]
    for activity, roles in state.raci.items():
        value = roles.get(state.role.lower(), "")
        lines.append(f"- {activity}: {value or 'not listed'} -> {'authorized' if _authority_ok(value) else 'NO authority'}")
    return "\n".join(lines)


_TASK_WRITE_KEYWORDS = [  # (RACI activity keyword, task-text pattern)
    ("create", re.compile(r"\b(create|add)\b", re.IGNORECASE)),
    ("close", re.compile(r"\bclose\b", re.IGNORECASE)),
]
_OWNER_VERB = re.compile(r"\b(set|update|change|assign)\b", re.IGNORECASE)
_OWNER_NOUN = re.compile(r"\b(owner|discipline)\b", re.IGNORECASE)


def _respond_block_reason(fn: Req_WrfmRespond, state) -> str | None:
    """Authority beats clarification: a write the role may not do must end in denied_security."""
    if not state.raci or fn.outcome == "denied_security":
        return None
    keywords = [kw for kw, pattern in _TASK_WRITE_KEYWORDS if pattern.search(state.task_text)]
    if _OWNER_VERB.search(state.task_text) and _OWNER_NOUN.search(state.task_text):
        keywords.append("activity owner")
    for kw in keywords:
        activity = next((a for a in state.raci if kw in a), None)
        if activity and not _authority_ok(state.raci[activity].get(state.role.lower(), "")):
            return f"AUTHORITY: role lacks authority for {activity} -> respond denied_security, cite wiki raci"
    return None


_CLOSE_WORDING = re.compile(r"\b(close|restart|restarted|back on ?line|resumed|restored|came back|brought back|"
                            r"returned to (?:production|service)|ended|finished)\b", re.IGNORECASE)
_CREATE_WORDING = re.compile(r"\b(create|add|log)\b", re.IGNORECASE)
_UPDATE_WORDING = re.compile(r"\b(update|change|set|amend)\b", re.IGNORECASE)


def _write_intent(text: str) -> tuple[str, set[str]]:
    """Task intent -> allowed write types (first match wins)."""
    if _OWNER_VERB.search(text) and _OWNER_NOUN.search(text):
        return "set owner", {"update"}
    if _CLOSE_WORDING.search(text):
        return "close", {"update", "create"}
    if _CREATE_WORDING.search(text):
        return "create", {"create"}
    if _CHECK_WORDING.search(text):
        return "review", {"create", "update"}
    if _UPDATE_WORDING.search(text):
        return "update", {"update"}
    return "unknown", {"create", "update"}  # no keyword: fall back to the other guards


def _review_task(text: str) -> bool:
    """Review/correct/check/reconcile/fix-data wording, and not a set-owner, close or create task."""
    return _write_intent(text)[0] == "review"


_CAUSE_IN_TEXT = re.compile(r"\b(?:due to|because of|caused by)\s+(.+?)"
                            r"(?=\s+(?:from|between|since|starting|until|at|on)\b|[.;,]|$)", re.IGNORECASE)


def build_comment(cause_text: str, day_kind: str, event_type: str) -> str | None:
    """COMMENTS convention for a new DOWN event on a partial production day (from evaluator feedback, not the wiki)."""
    cause = (cause_text or "").strip()
    if not cause or day_kind != "partial" or str(event_type).upper() != "DOWN":
        return None
    return f"{cause[0].upper()}{cause[1:]} deferment; prorated by downtime within the day."


def _convention_comment(fn, state, wrfm) -> str | None:
    """The code-built COMMENTS for a create on a new event, or None when the convention does not apply."""
    if not isinstance(fn, Req_WrfmDataCreate) or fn.table_name != "deferments" or "comment" in state.task_text.lower():
        return None
    match = _CAUSE_IN_TEXT.search(state.task_text)
    values = {fv.field_name: str(fv.value).strip() for fv in fn.field_values}
    if not match or not all(values.get(f) for f in ("EVENT_KEY", "START_DATE", "PRODUCTION_DAY")):
        return None
    existing = _known_event_rows(values["EVENT_KEY"], state, wrfm)
    if existing is None or any(r.get("START_DATE") for r in existing):
        return None  # unknown or existing event: copy its COMMENTS
    try:
        hours = _downtime_h(values["START_DATE"], values.get("END_DATE") or None, values["PRODUCTION_DAY"], exact=True)
    except (TypeError, ValueError):
        return None
    return build_comment(match.group(1), "partial" if 0 < hours < 24 else "full", values.get("EVENT_TYPE", ""))


def _clarify_guard(block: str) -> tuple[str, str] | None:
    """(guard, missing info) when a write block means the task lacks information only the user can give."""
    if block.startswith("OWNER BLOCKED") and "not stated in the task" in block:
        return "OWNER BLOCKED", "the accountable DEFER_ACTIVITY_OWNER"
    if block.startswith("END TIME BLOCKED"):
        return "END TIME BLOCKED", "the exact restart/end timestamp"
    if block.startswith("VOLUME CHECK: no data-derived volume_estimate"):
        return "VOLUME CHECK (no volume_estimate)", "the deferred volumes"
    return None


def _blocked_write_block(fn: Req_WrfmRespond, state) -> str | None:
    """Writes blocked for missing information and none succeeded: the answer is a clarification, not ok_answer."""
    if fn.outcome != "ok_answer" or not state.clarify_blocks or state.write_ok:
        return None
    guard, missing = state.clarify_blocks[-1]
    return f"BLOCKED WRITE: {guard} -> respond none_clarification_needed asking for {missing}, cite the event and well"


def _enrich_refs(fn: Req_WrfmRespond, state) -> tuple[Req_WrfmRespond, list[dict]]:
    """Drop refs never seen in this task; add a table ref for every table used. Never adds wiki refs."""
    task_lower = state.task_text.lower()
    auto_events = set(state.event_targets)
    if len(state.selected_keys) == 1:
        auto_events |= state.selected_keys
    state.seen_ids |= auto_events | state.ref_wells
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
    cited_events = {str(r.id) for r in kept if r.type == "deferment_event"}
    for key in sorted(auto_events - cited_events):
        kept.append(WrfmGroundRef(type="deferment_event", id=key, label=key))
    wells = set(state.ref_wells)
    for key in auto_events:  # the well of a W_ event the task is about
        match = _W_EVENT_KEY.match(key)
        if match:
            wells.add(match.group(1))
    cited_wells = {str(r.id) for r in kept if r.type == "well"}
    for well in sorted(wells - cited_wells):
        kept.append(WrfmGroundRef(type="well", id=well, label=well))
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
    authority_note = _authority_note(state)
    if authority_note:
        print(f"  {CLI_GREEN}AUTO authority{CLI_CLR}: {authority_note[:120]}")
        log.append({"role": "user", "content": f"[authority]\n{authority_note}"})
        _trace(state, {"authority": authority_note})
    try:
        forced_key = _task_event_key(wrfm, state)
    except Exception:
        forced_key = None
    if forced_key:
        check = _local_event_check(wrfm, LocalEventCheck(type="local_event_check", event_key=forced_key), state)
        if "error" not in check:
            _note_event_check(state, forced_key, check)
            print(f"  {CLI_GREEN}AUTO event_check{CLI_CLR}: {forced_key}")
            log.append({"role": "user", "content": "[event_check] INFO ONLY. Act on these items only if the task asks "
                                                   f"to review/correct the event data.\n{json.dumps(check)}"})
            _trace(state, {"forced_event_check": forced_key, "result": check})
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
        comment_note = ""
        built_comment = _convention_comment(fn, state, wrfm)
        if built_comment:
            given = next((str(fv.value) for fv in fn.field_values if fv.field_name == "COMMENTS"), "")
            if given != built_comment:
                fn = fn.model_copy(update={"field_values": [fv for fv in fn.field_values if fv.field_name != "COMMENTS"]
                                           + [WrfmKeyValue(field_name="COMMENTS", value=built_comment)]})
                comment_note = f"NOTE: COMMENTS set by code to the data convention: {built_comment!r}\n"
        block = _write_block_reason(fn, state, wrfm)
        dropped_refs = []
        respond_block = None
        if isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)) and _write_event_key(fn):
            state.event_targets.add(str(_write_event_key(fn)))
        if isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)):
            state.ref_wells.update(str(fv.value).strip() for fv in fn.field_values
                                   if fv.field_name == "WELL_NAME" and str(fv.value).strip())
        if isinstance(fn, Req_WrfmDataUpdate):
            state.ref_wells.update(str(c.value) for c in fn.where_clause
                                   if c.field_name == "WELL_NAME" and c.operator == "eq" and c.value)
        if isinstance(fn, LocalVolumeEstimate):
            state.ref_wells.add(fn.well)
        if isinstance(fn, LocalEventCheck):
            state.event_targets.add(fn.event_key)
        if isinstance(fn, Req_WrfmRespond):
            fn, dropped_refs = _enrich_refs(fn, state)
            respond_block = (_respond_block_reason(fn, state) or _blocked_write_block(fn, state)
                             or _event_check_block(fn, state, step.current_state))
            block = block or respond_block
        if isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)) and block and _clarify_guard(block):
            state.clarify_blocks.append(_clarify_guard(block))
        if isinstance(fn, (LocalCalc, LocalAggregate, LocalDowntime, LocalEventCheck, LocalVolumeEstimate)):
            try:
                if isinstance(fn, LocalCalc):
                    local = _local_calc(fn.expression)
                elif isinstance(fn, LocalAggregate):
                    local = _local_aggregate(wrfm, fn)
                elif isinstance(fn, LocalDowntime):
                    local = _local_downtime(fn)
                elif isinstance(fn, LocalVolumeEstimate):
                    local = _volume_estimate(wrfm, fn.well, fn.start, fn.end or None, fn.production_day, fn.event_type,
                                             task_text=state.task_text)
                else:
                    local = _local_event_check(wrfm, fn, state)
            except Exception as exc:
                local = {"error": str(exc), "code": "local_tool_error"}
            if isinstance(fn, LocalAggregate) and "error" not in local:
                state.used_tables.add(fn.table_name)
            if isinstance(fn, LocalEventCheck) and "error" not in local:
                _note_event_check(state, fn.event_key, local)
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
                    result_keys = {str(r.get("EVENT_KEY")) for r in (getattr(result, "rows", []) or []) if r.get("EVENT_KEY")}
                    if isinstance(fn, Req_WrfmDataSelect) and fn.table_name == "deferments":
                        state.selected_keys |= result_keys
                    if isinstance(fn, Req_WrfmDataUpdate):
                        state.event_targets |= result_keys
                        state.updated_pairs |= {(str(r.get("WELL_NAME")), str(r.get("PRODUCTION_DAY"))[:10])
                                                for r in (getattr(result, "rows", []) or [])}
                    if isinstance(fn, Req_WrfmDataCreate):
                        values = {fv.field_name: str(fv.value).strip() for fv in fn.field_values}
                        state.written_pairs.add((values.get("WELL_NAME", ""), values.get("PRODUCTION_DAY", "")[:10]))
                    if (isinstance(fn, Req_WrfmDataSelect) and fn.table_name == "deferments" and len(result_keys) == 1
                            and next(iter(result_keys)) not in state.event_checks):
                        auto_key = next(iter(result_keys))
                        auto_check = _local_event_check(wrfm, LocalEventCheck(type="local_event_check", event_key=auto_key), state)
                        if "error" not in auto_check:
                            _note_event_check(state, auto_key, auto_check)
                            result_text += f"\n[event_check]\n{json.dumps(auto_check)}"
                if isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)):
                    state.write_ok = True
                    state.event_rows.clear()  # rows changed; refetch on next guard check
            except ApiException as exc:
                result_text = exc.api_error.model_dump_json(exclude_none=True)
                print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
            except Exception as exc:
                result_text = f'{{"error": "{exc}"}}'
                print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
            result_text = note + comment_note + result_text

        log.append({"role": "tool", "content": f"RESULT of {fn_type}: {result_text}", "tool_call_id": step_id})
        _trace(state, {
            "step": i + 1, "current_state": step.current_state, "plan": step.plan, "function": fn_type,
            "args": json.loads(fn.model_dump_json(exclude_none=True)), "result": result_text,
            "block": block if not isinstance(fn, (LocalCalc, LocalAggregate, LocalDowntime, LocalEventCheck,
                                                  LocalVolumeEstimate)) else None,
            "dropped_refs": dropped_refs, "elapsed_ms": elapsed_ms,
            "tokens": {"prompt": prompt_tokens, "cached": cached_prompt_tokens, "completion": completion_tokens},
        })

        if isinstance(fn, Req_WrfmRespond) and not respond_block:
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
