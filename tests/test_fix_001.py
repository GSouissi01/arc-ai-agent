"""Offline test of fix-001 (agent.py parts): no network."""
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from ogchallenge_client import ApiException
from ogchallenge_client.dtos import (
    ApiError, Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmDataUpdate,
    Req_WrfmRespond, Resp_WrfmRespond, WrfmKeyValue,
)
import agent


def step(fn):
    return agent.NextStep(current_state="st", plan=["p"], function=fn)


def create(**fv):
    return Req_WrfmDataCreate(table_name="deferments",
                              field_values=[WrfmKeyValue(field_name=k, value=v) for k, v in fv.items()])


class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs, self.kwargs = list(script), [], []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        self.kwargs.append(kw)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class FakeWrfm:
    def __init__(self, schema):
        self.schema, self.dispatched = schema, []

    def system(self): raise RuntimeError("offline")
    def wiki_tree(self): raise RuntimeError("offline")

    def data_schema(self):
        return SimpleNamespace(model_dump_json=lambda: json.dumps(self.schema))

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WrfmDataSelect) and fn.table_name == "bad":
            raise ApiException(ApiError(status=400, error="unknown field X", code="bad_field"))
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None):
        pass


def run(script, schema=None):
    llm = FakeLLM(script)
    wrfm = FakeWrfm(schema or {"tables": {"deferments": {"recommended_filters": ["WELL", "EVENT_KEY"]}}})
    api = SimpleNamespace(get_wrfm_client=lambda t: wrfm, log_llm=lambda **k: None)
    agent.make_llm_client = lambda cfg: llm
    agent.run_agent(api, SimpleNamespace(spec_id="s", task_id="t", task_text="Add the missing deferment rows for WELL-03 yesterday."),
                    llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
    return llm, wrfm


fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


RESPOND = step(Req_WrfmRespond(message="ok", outcome="ok_answer"))
GOV = step(Req_WikiLoad(path="governance/raci.md"))

# A. Write before governance is blocked, not dispatched.
llm, wrfm = run([step(create(WELL="W1", EVENT_KEY="E1")), RESPOND])
check("A create blocked without governance", not any(isinstance(f, Req_WrfmDataCreate) for f in wrfm.dispatched))
check("A block message in history", "WRITE BLOCKED" in str(llm.inputs[1]) and "blocked_by_agent_guard" in str(llm.inputs[1]))

# B. Update before governance also blocked.
llm, wrfm = run([step(Req_WrfmDataUpdate(table_name="deferments")), RESPOND])
check("B update blocked without governance", not any(isinstance(f, Req_WrfmDataUpdate) for f in wrfm.dispatched))

# C. After governance: first create passes, duplicate (same recommended keys, different volume) blocked.
llm, wrfm = run([GOV, step(create(WELL="W1", EVENT_KEY="E1", VOL="0")),
                 step(create(WELL="W1", EVENT_KEY="E1", VOL="5")), RESPOND])
creates = [f for f in wrfm.dispatched if isinstance(f, Req_WrfmDataCreate)]
check("C one create dispatched", len(creates) == 1)
check("C duplicate message", "DUPLICATE WRITE BLOCKED" in str(llm.inputs[3]))

# D. Different event key is allowed.
llm, wrfm = run([GOV, step(create(WELL="W1", EVENT_KEY="E1")), step(create(WELL="W1", EVENT_KEY="E2")), RESPOND])
check("D distinct creates allowed", sum(isinstance(f, Req_WrfmDataCreate) for f in wrfm.dispatched) == 2)

# E. No recommended filters -> full-pair signature (VOL differs -> allowed).
llm, wrfm = run([GOV, step(create(WELL="W1", VOL="0")), step(create(WELL="W1", VOL="5")), RESPOND],
                schema={"tables": {}})
check("E fallback full-pair key", sum(isinstance(f, Req_WrfmDataCreate) for f in wrfm.dispatched) == 2)

# F. History shows executed call + RESULT prefix; temperature=0 passed.
llm, wrfm = run([step(Req_WrfmDataSelect(table_name="t1")), RESPOND])
h = str(llm.inputs[1])
check("F EXECUTED + STATE in history", "EXECUTED: wrfm_data_select" in h and "STATE: st" in h and '"table_name":"t1"' in h)
check("F RESULT prefix", "RESULT of wrfm_data_select:" in h)
check("F temperature=0", all(k.get("temperature") == 0 for k in llm.kwargs))

# G. ApiException full detail (status included).
llm, wrfm = run([step(Req_WrfmDataSelect(table_name="bad")), RESPOND])
check("G full api error", '"status":400' in str(llm.inputs[1]) and "bad_field" in str(llm.inputs[1]))

# H. Bad schema text -> recommended empty, no crash.
llm, wrfm = run([RESPOND], schema="not-a-dict")
check("H bad schema tolerated", True)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
