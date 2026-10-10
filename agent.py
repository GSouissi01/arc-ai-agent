"""
WRFM sample agent - structured-output loop.

Uses OpenAI structured output with provider-specific Pydantic schema variants
over the WRFM API request models. WrfmClient.dispatch() handles routing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Annotated, List, Union

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
Do not mutate records when the task asks only for review, anomaly checking, or clarification.
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

    bootstrap_log = _bootstrap(wrfm)

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
        if block:
            result_text = json.dumps({"error": block, "code": "blocked_by_agent_guard"})
            print(f"    {CLI_RED}BLOCKED: {block}{CLI_CLR}")
        else:
            try:
                result = wrfm.dispatch(fn)
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


def _bootstrap(wrfm: WrfmClient) -> list[tuple[str, str]]:
    results = []

    try:
        system = wrfm.system()
        results.append(("system", system.model_dump_json()))
    except Exception as exc:
        results.append(("system", f"error: {exc}"))

    try:
        wiki = wrfm.wiki_tree()
        results.append(("wiki_tree", wiki.tree))
    except Exception as exc:
        results.append(("wiki_tree", f"error: {exc}"))

    try:
        schema = wrfm.data_schema()
        results.append(("data_schema", schema.model_dump_json()))
    except Exception as exc:
        results.append(("data_schema", f"error: {exc}"))

    return results
