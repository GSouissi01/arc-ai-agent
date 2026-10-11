"""Offline test of Fix A: fake LLM + fake WRFM client, no network."""
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import httpx2 as httpx
from openai import BadRequestError
from ogchallenge_client import ApiException
from ogchallenge_client.dtos import Req_WrfmDataSelect, Req_WrfmRespond, Resp_WrfmRespond
import agent


def tool_use_failed():
    req = httpx.Request("POST", "http://fake")
    return BadRequestError(
        "Tool choice is none, but model called a tool",
        response=httpx.Response(400, request=req),
        body={"message": "Tool choice is none", "code": "tool_use_failed"},
    )


def step(fn):
    return agent.NextStep(current_state="s", plan=["p"], function=fn)


SELECT = step(Req_WrfmDataSelect(table_name="deferments"))
RESPOND = step(Req_WrfmRespond(message="done", outcome="ok_answer"))


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.inputs = []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(output_parsed=item, usage=None)


class FakeWrfm:
    def __init__(self, reject_respond=False):
        self.dispatched_responds = 0
        self.fallbacks = []
        self.reject_respond = reject_respond

    def system(self): raise RuntimeError("offline")
    def wiki_tree(self): raise RuntimeError("offline")
    def data_schema(self): raise RuntimeError("offline")

    def dispatch(self, fn):
        if isinstance(fn, Req_WrfmRespond):
            self.dispatched_responds += 1
            if self.reject_respond:
                raise RuntimeError("respond rejected")
            return Resp_WrfmRespond()
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None):
        self.fallbacks.append((outcome, message))


def run(script, reject_respond=False):
    llm = FakeLLM(script)
    wrfm = FakeWrfm(reject_respond)
    api = SimpleNamespace(get_wrfm_client=lambda t: wrfm, log_llm=lambda **k: None)
    task = SimpleNamespace(spec_id="test_spec", task_id="t1", task_text="do it")
    agent.make_llm_client = lambda cfg: llm
    cfg = agent.LLMConfig(provider="openai", model="m", api_key="k")
    err = None
    try:
        agent.run_agent(api, task, llm_config=cfg)
    except Exception as exc:
        err = exc
    return llm, wrfm, err


failures = 0
def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


# 1. Two tool_use_failed then success -> normal respond, no fallback, history clean.
llm, wrfm, err = run([tool_use_failed(), tool_use_failed(), RESPOND])
check("1 recovers after 2 retries", err is None and wrfm.dispatched_responds == 1 and not wrfm.fallbacks)
check("1 retry prompt only on retries", agent.FORMAT_RETRY_PROMPT not in str(llm.inputs[0])
      and agent.FORMAT_RETRY_PROMPT in str(llm.inputs[1]) and agent.FORMAT_RETRY_PROMPT in str(llm.inputs[2]))

# 2. Three tool_use_failed -> LLMFormatError, exactly one error_internal fallback.
llm, wrfm, err = run([tool_use_failed()] * 3)
check("2 LLMFormatError raised", isinstance(err, agent.LLMFormatError))
check("2 one fallback error_internal", [o for o, _ in wrfm.fallbacks] == ["error_internal"])

# 3. Parse returns None twice then success.
llm, wrfm, err = run([None, None, RESPOND])
check("3 parse_none recovered", err is None and not wrfm.fallbacks)

# 4. Non-format LLM error -> no retry, one fallback.
llm, wrfm, err = run([ValueError("boom")])
check("4 other error not retried", len(llm.inputs) == 1 and isinstance(err, RuntimeError))
check("4 one fallback", len(wrfm.fallbacks) == 1)

# 5. MAX_STEPS reached -> one fallback with max-steps reason.
agent.MAX_STEPS = 3
llm, wrfm, err = run([SELECT] * 3)
check("5 max steps fallback", err is None and len(wrfm.fallbacks) == 1 and "max steps" in wrfm.fallbacks[0][1])

# 6. Agent respond rejected -> one fallback, reason mentions rejection.
llm, wrfm, err = run([RESPOND], reject_respond=True)
check("6 rejected respond -> fallback", len(wrfm.fallbacks) == 1 and "rejected" in wrfm.fallbacks[0][1])

# 7. Success after select -> no fallback.
llm, wrfm, err = run([SELECT, RESPOND])
check("7 normal path no fallback", err is None and wrfm.dispatched_responds == 1 and not wrfm.fallbacks)

print("ALL PASS" if not failures else f"{failures} FAILED")
sys.exit(failures)
