"""
WRFM sample agent - structured-output loop.

Uses OpenAI structured output with provider-specific Pydantic schema variants
over the WRFM API request models. WrfmClient.dispatch() handles routing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
import operator
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
4. Deferment rules:
   - One row per production day per event. Event-level fields are identical across the event's rows.
   - Adding a missing day row for an existing event: copy EVENT_KEY, START_DATE, cause, system group, category, event type, asset type and activity owner from that event's existing rows.
   - New event: build EVENT_KEY exactly as the deferment guide says, matching the format of existing keys. Use approved labels only, choosing the closest approved label for the stated cause and system. Leave fields empty that the task and data do not give, unless they are in the guide's minimum information.
   - Closing an event: set the same END_DATE on every row of the event, and make sure a row exists for every production day from the start day to the end day; create the missing ones.
   - Deferred volumes = daily rate × (downtime hours inside that production day ÷ 24). Get the daily rate from an existing full-day row of the same event if one exists, otherwise from the well's daily_production before the event. Compute with local_calc. Never write 0 or a placeholder volume.
5. Each step executes its function immediately. A create or update that returned a row is done; never repeat it.
6. Data: select one table at a time. Do not use order_by. Use local_aggregate for totals, maximums and per-event sums, and local_calc for every arithmetic step. Never do arithmetic in your head.
7. Tool errors: read the error and change the call. Never repeat an identical failing call. A tool error is never a reason for denied_security or none_unsupported.
8. Ground refs: cite wiki docs by full path, tables by name, wells by WELL_NAME, and deferment events by EVENT_KEY. Cite only items you actually used in this task.
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


def _create_key(fn, state):
    keys = state.recommended.get(fn.table_name, set())
    pairs = [(fv.field_name, str(fv.value)) for fv in fn.field_values]
    sig = tuple(sorted(p for p in pairs if p[0] in keys)) or tuple(sorted(pairs))
    return (fn.table_name, sig)


def _write_block_reason(fn, state):
    if not isinstance(fn, (Req_WrfmDataCreate, Req_WrfmDataUpdate)):
        return None
    if not any("governance" in p.lower() for p in state.loaded_docs):
        return ("WRITE BLOCKED: load the governance (RACI) wiki document and confirm your role "
                "is authorized for this activity before any create or update.")
    if isinstance(fn, Req_WrfmDataCreate) and _create_key(fn, state) in state.create_keys:
        return ("DUPLICATE WRITE BLOCKED: this row was already created earlier in this task. "
                "Do not repeat it. Verify with a select, or respond.")
    return None


ORDER_BY_NOTE = ("NOTE: order_by is not supported and was removed; rows are unsorted. "
                 "Use local_aggregate for max/min/sum.")

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

    bootstrap_log = _bootstrap(wrfm, state)

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
            except Exception:
                state.recommended = {}

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
        block = _write_block_reason(fn, state)
        if isinstance(fn, (LocalCalc, LocalAggregate)):
            try:
                local = _local_calc(fn.expression) if isinstance(fn, LocalCalc) else _local_aggregate(wrfm, fn)
            except Exception as exc:
                local = {"error": str(exc), "code": "local_tool_error"}
            result_text = json.dumps(local)
            style = CLI_RED if "error" in local else CLI_GREEN
            print(f"    {style}->{CLI_CLR} {result_text[:200]}")
        elif block:
            result_text = json.dumps({"error": block, "code": "blocked_by_agent_guard"})
            print(f"    {CLI_RED}BLOCKED: {block}{CLI_CLR}")
        else:
            note = ""
            to_send = fn
            if isinstance(fn, Req_WrfmDataSelect) and fn.order_by:
                to_send = fn.model_copy(update={"order_by": None})
                note = ORDER_BY_NOTE + "\n"
            try:
                result = wrfm.dispatch(to_send)
                result_text = result.model_dump_json(exclude_none=True)
                dispatch_ok = True
                print(f"    {CLI_GREEN}->{CLI_CLR} {result_text[:200]}")
                if isinstance(fn, Req_WikiLoad):
                    state.loaded_docs.add(fn.path)
                if isinstance(fn, Req_WrfmDataCreate):
                    state.create_keys.add(_create_key(fn, state))
            except ApiException as exc:
                result_text = exc.api_error.model_dump_json(exclude_none=True)
                print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
            except Exception as exc:
                result_text = f'{{"error": "{exc}"}}'
                print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
            result_text = note + result_text

        log.append({"role": "tool", "content": f"RESULT of {fn_type}: {result_text}", "tool_call_id": step_id})

        if isinstance(fn, Req_WrfmRespond):
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
