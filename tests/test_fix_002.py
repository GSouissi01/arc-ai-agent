"""Offline test of fix-002: no network."""
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from ogchallenge_client import ApiException
from ogchallenge_client.dtos import (
    ApiError, Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmRespond,
    Resp_WrfmRespond, WrfmCondition, WrfmKeyValue,
)
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


# ---- local_calc ----
r = agent._local_calc("2380000 * 14.75 / 24")
check("calc basic", abs(r["result"] - 1462708.3333333) < 1e-3 and r["rounded_2dp"] == 1462708.33)
check("calc unary/parens", agent._local_calc("-(3 + 4) * 2")["result"] == -14.0)
for bad in ["__import__('os').system('x')", "2 ** 3", "a + 1", "True + 1", "'1' + '2'", "[1][0]", "1 // 2", "+5"]:
    check(f"calc rejects {bad!r}", agent._local_calc(bad).get("code") == "local_calc_error")
check("calc div by zero", agent._local_calc("1/0").get("code") == "local_calc_error")


# ---- local_aggregate ----
class PagingWrfm:
    def __init__(self, pages, fail=False):
        self.pages, self.calls, self.fail = pages, [], fail

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        self.calls.append(dict(table=table_name, fields=fields, where=where_clause, offset=offset, limit=limit))
        if self.fail:
            raise ApiException(ApiError(status=400, error="unknown field", code="unknown_field"))
        idx = [p[0] for p in self.pages].index(offset)
        _, rows, nxt = self.pages[idx]
        return SimpleNamespace(rows=rows, next_offset=nxt)


pages = [
    (0, [{"EVENT_KEY": "A", "V": "10"}, {"EVENT_KEY": "B", "V": "5"}], 2),
    (2, [{"EVENT_KEY": "A", "V": "7.5"}, {"EVENT_KEY": "B", "V": ""}], 4),
    (4, [{"EVENT_KEY": "C", "V": None}, {"EVENT_KEY": "B", "V": 1}], -1),
]
w = PagingWrfm(pages)
where = [WrfmCondition(field_name="WELL_NAME", operator="eq", value="X")]
fn = agent.LocalAggregate(type="local_aggregate", table_name="deferments", where_clause=where,
                          group_by="EVENT_KEY", value_field="V", op="sum")
res = agent._local_aggregate(w, fn)
check("agg paged 3 times", [c["offset"] for c in w.calls] == [0, 2, 4])
check("agg fields + where passed", w.calls[0]["fields"] == ["EVENT_KEY", "V"] and w.calls[0]["where"] == where)
vals = {g["group"]: (g["value"], g["rows"]) for g in res["groups"]}
check("agg sums skip empties", vals == {"A": (17.5, 2), "B": (6.0, 3), "C": (None, 1)})
check("agg sorted desc, None last", [g["group"] for g in res["groups"]] == ["A", "B", "C"])
check("agg top + total_rows", res["top"]["group"] == "A" and res["total_rows"] == 6)

w = PagingWrfm(pages)
res = agent._local_aggregate(w, agent.LocalAggregate(type="local_aggregate", table_name="t", value_field="V", op="max"))
check("agg whole selection max", res["groups"] == [{"group": "(all)", "value": 10.0, "rows": 6}]
      and w.calls[0]["fields"] == ["V"])
res = agent._local_aggregate(PagingWrfm(pages), agent.LocalAggregate(
    type="local_aggregate", table_name="t", group_by="EVENT_KEY", value_field="V", op="count"))
check("agg count non-empty", {g["group"]: g["value"] for g in res["groups"]} == {"A": 2.0, "B": 2.0, "C": 0.0})
res = agent._local_aggregate(PagingWrfm(pages, fail=True), fn)
check("agg api error returned", res.get("code") == "unknown_field")


# ---- loop-level: bootstrap wiki, order_by guard, local tools ----
TREE = ".\n|-- data_reference/\n|   `-- schema.md\n|-- governance/\n|   `-- raci.md\n`-- system_reference/\n    `-- system.md\n"


class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class FakeWrfm:
    def __init__(self, tree=TREE, fail_path="system_reference/system.md"):
        self.tree, self.fail_path, self.dispatched = tree, fail_path, []

    def system(self): return SimpleNamespace(model_dump_json=lambda: '{"role":"Production Programmer"}')
    def wiki_tree(self, path=""): return SimpleNamespace(tree=self.tree)
    def data_schema(self): return SimpleNamespace(model_dump_json=lambda: '{"tables": {}}')

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WikiLoad):
            if fn.path == self.fail_path:
                raise ApiException(ApiError(status=404, error="wiki_not_found", code="wiki_not_found"))
            return SimpleNamespace(content=f"CONTENT OF {fn.path}")
        return Resp_WrfmRespond()

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        return SimpleNamespace(rows=[{"V": "3"}], next_offset=-1)

    def respond(self, message, outcome, ground_refs=None): pass


def step(fn):
    return agent.NextStep(current_state="st", plan=["p"], function=fn)


def run(script, wrfm=None):
    llm, wrfm = FakeLLM(script), wrfm or FakeWrfm()
    api = SimpleNamespace(get_wrfm_client=lambda t: wrfm, log_llm=lambda **k: None)
    agent.make_llm_client = lambda cfg: llm
    agent.run_agent(api, SimpleNamespace(spec_id="s", task_id="t", task_text="Add the missing deferment row for WELL-01 yesterday."),
                    llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
    return llm, wrfm


RESPOND = step(Req_WrfmRespond(message="ok", outcome="ok_answer"))

llm, wrfm = run([RESPOND])
first = llm.inputs[0]
texts = [m["content"] for m in first]
order = [t.split("\n")[0] for t in texts]
check("bootstrap wiki loaded after data_schema", order.index("[data_schema]") < order.index("[wiki:data_reference/schema.md]")
      < order.index("[wiki:governance/raci.md]") < order.index("Add the missing deferment row for WELL-01 yesterday."))
check("bootstrap wiki content", "[wiki:governance/raci.md]\nCONTENT OF governance/raci.md" in texts)
check("bootstrap load error kept, no crash", any(t.startswith("[wiki:system_reference/system.md]\nerror:") for t in texts))
check("bootstrap uses dispatch(Req_WikiLoad)", [f.path for f in wrfm.dispatched if isinstance(f, Req_WikiLoad)]
      == ["data_reference/schema.md", "governance/raci.md", "system_reference/system.md"])

# governance satisfied from bootstrap -> create dispatched
create = Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name="WELL_NAME", value="X")])
llm, wrfm = run([step(create), RESPOND])
check("governance guard satisfied by bootstrap", any(isinstance(f, Req_WrfmDataCreate) for f in wrfm.dispatched))

# governance missing in tree -> still blocked
llm, wrfm = run([step(create), RESPOND], wrfm=FakeWrfm(tree="data_reference/schema.md"))
check("guard still blocks without governance doc", not any(isinstance(f, Req_WrfmDataCreate) for f in wrfm.dispatched))

# order_by removed + note
llm, wrfm = run([step(Req_WrfmDataSelect(table_name="deferments", order_by="X desc")), RESPOND])
sent = [f for f in wrfm.dispatched if isinstance(f, Req_WrfmDataSelect)][0]
check("order_by direction dropped before dispatch", sent.order_by == "X" and sent.table_name == "deferments")
check("order_by note in result", "RESULT of wrfm_data_select: NOTE: order_by direction is not supported" in str(llm.inputs[1]))

# local tools in loop: not dispatched, results in history
llm, wrfm = run([step(agent.LocalCalc(type="local_calc", expression="2380000*10.75/24")),
                 step(agent.LocalAggregate(type="local_aggregate", table_name="t", value_field="V", op="sum")),
                 RESPOND])
check("local tools never dispatched", not any(isinstance(f, (agent.LocalCalc, agent.LocalAggregate)) for f in wrfm.dispatched))
h = str(llm.inputs[2])
check("local_calc result in history", "EXECUTED: local_calc" in h and "1066041.67" in h)
check("local_aggregate result in history", "RESULT of local_aggregate" in str(llm.inputs[2]) and '"value": 3.0' in h)

# prompt
check("prompt: removed line gone", "Do not mutate records when the task asks only" not in agent.SYSTEM_PROMPT)
check("prompt: checklist + volume rule", "Decision checklist" in agent.SYSTEM_PROMPT
      and "Deferred volumes" in agent.SYSTEM_PROMPT and "<VOLUME RULE>" not in agent.SYSTEM_PROMPT)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
