"""Offline test of fix-014 (answer-only bare-number display format): no network."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
from ogchallenge_client.dtos import Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmRespond, Resp_WrfmRespond, WrfmKeyValue
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


T = {t: json.loads(Path(ROOT, f"runs/data/{t}.json").read_text(encoding="utf-8"))
     for t in ("deferments", "daily_production", "well_status_history", "well_info")}


class DW:
    def __init__(self, tables):
        self.tables = tables

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        rows = [r for r in self.tables.get(table_name, []) if all(str(r.get(c.field_name)) == str(c.value) for c in (where_clause or []))]
        return SimpleNamespace(rows=rows, next_offset=-1)


SCHEMA = json.loads(Path(ROOT, "runs/wiki/data_schema.json").read_text(encoding="utf-8"))
RACI_MD = Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8")
RACI = agent._parse_raci(RACI_MD)


def mk_state(task):
    s = agent._TaskState(role="Production Programmer", raci=RACI, task_text=task)
    for table, info in SCHEMA["tables"].items():
        f = info.get("fields") or {}
        s.required[table] = [k for k, v in f.items() if v.get("required")]
        s.enums[table] = {k: v["enum"] for k, v in f.items() if v.get("enum")}
    return s

# loop: auto event check on a single-event select, respond guard, override with reason
class LLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class LoopW(DW):
    def __init__(self):
        super().__init__(T)
        self.dispatched = []

    def system(self): return SimpleNamespace(model_dump_json=lambda: json.dumps({"role": "Production Programmer", "today": "2025-10-15"}))
    def wiki_tree(self, path=""): return SimpleNamespace(tree=".\n`-- governance/\n    `-- raci.md\n")
    def data_schema(self): return SimpleNamespace(model_dump_json=lambda: json.dumps(SCHEMA))

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WikiLoad):
            return SimpleNamespace(content=RACI_MD)
        if isinstance(fn, Req_WrfmDataSelect):
            rows = [r for r in T["deferments"] if r["EVENT_KEY"] == "G_NOVA7_2025-10-09-1"]
            return SimpleNamespace(rows=rows, model_dump_json=lambda exclude_none=True: json.dumps({"rows": rows}))
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None): pass


def step(fn, cs="s"):
    return agent.NextStep(current_state=cs, plan=["p"], function=fn)

from ogchallenge_client.dtos import WrfmGroundRef, WrfmCondition
T004 = "Summarise the largest deferment event. Give only deferred gas volume figure."
TCOUNT = "How many wells were affected by the largest event? Give only the number."
def r(msg, outcome="ok_answer"):
    return Req_WrfmRespond(message=msg, outcome=outcome, ground_refs=[])
f = lambda msg, text, outcome="ok_answer": agent._format_bare_number(r(msg, outcome), SimpleNamespace(task_text=text))
check("014 volume 14717986.11 -> 14,717,986.11", f("14717986.11", T004) == "14,717,986.11")
check("014 volume 1190000.0 -> 1,190,000.00", f("1190000.0", T004) == "1,190,000.00")
check("014 volume integral 14717986 -> 14,717,986.00", f("14717986", T004) == "14,717,986.00")
check("014 count 5 -> 5", f("5", TCOUNT) == "5")
check("014 count 12345 -> 12,345", f("12345", TCOUNT) == "12,345")
check("014 non-integral non-volume -> 2 decimals", f("1234.5", TCOUNT) == "1,234.50")
check("014 text message unchanged", f("The largest event deferred 14717986.11 Sm3.", T004) is None)
check("014 non answer-only task unchanged", f("14717986.11", "Summarise the largest deferment event.") is None)
check("014 clarification unchanged", f("14717986.11", T004, "none_clarification_needed") is None)

# loop: tool args stay raw, respond message formatted
sel = Req_WrfmDataSelect(table_name="deferments", where_clause=[WrfmCondition(field_name="DEFERRED_GAS_VOL_SM3", operator="eq", value="1190000.0")])
llm, w = LLM([step(sel), step(Req_WrfmRespond(message="14717986.11", outcome="ok_answer",
                                              ground_refs=[WrfmGroundRef(type="table", id="deferments")]))]), LoopW()
agent.make_llm_client = lambda cfg: llm
agent.run_agent(SimpleNamespace(get_wrfm_client=lambda t: w, log_llm=lambda **k: None),
                SimpleNamespace(spec_id="zz_test_014", task_id="t", task_text=T004),
                llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
sent_sel = next(x for x in w.dispatched if isinstance(x, Req_WrfmDataSelect))
sent_resp = next(x for x in w.dispatched if isinstance(x, Req_WrfmRespond))
check("014 loop: tool args raw (where value 1190000.0)", sent_sel.where_clause[0].value == "1190000.0")
check("014 loop: respond message sent formatted", sent_resp.message == "14,717,986.11")
check("014 prompt rule 9 updated", "Bare-number answers are formatted by the system; output the raw value only." in agent.SYSTEM_PROMPT
      and "no thousands separators" not in agent.SYSTEM_PROMPT)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
