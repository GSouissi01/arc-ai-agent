"""Offline test of fix-004 Step 4 (volume validator), order_by direction, Step 8 prompt: no network."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
from ogchallenge_client.dtos import Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmRespond, Resp_WrfmRespond, WrfmKeyValue
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


SCHEMA = json.loads(Path(ROOT, "runs/wiki/data_schema.json").read_text(encoding="utf-8"))
RACI = agent._parse_raci(Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8"))
REAL = json.loads(Path(ROOT, "runs/data/deferments.json").read_text(encoding="utf-8"))


def mk_state(task="Review and correct the deferment event data."):
    s = agent._TaskState(role="Production Programmer", raci=RACI, task_text=task)
    for table, info in SCHEMA["tables"].items():
        fields = info.get("fields") or {}
        s.required[table] = [f for f, spec in fields.items() if spec.get("required")]
        s.enums[table] = {f: spec["enum"] for f, spec in fields.items() if spec.get("enum")}
    return s


class DB:
    def __init__(self, tables):
        self.tables = tables

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        rows = [r for r in self.tables.get(table_name, [])
                if all(str(r.get(c.field_name)) == str(c.value) for c in (where_clause or []))]
        return SimpleNamespace(rows=rows, next_offset=-1)


def create(**f):
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=str(v)) for k, v in f.items()])


# (a) existing event: real W_WELL-03 event rows (rate 2,380,000 / 35.8), new ongoing full day
ev = [r for r in REAL if r["EVENT_KEY"] == "W_WELL-03_2025-09-14-1"]
base = {k: ev[0][k] for k in ("EVENT_KEY", "WELL_NAME", "DEFER_CATEGORY_IND", "START_DATE", "END_DATE", "EVENT_TYPE", "DEFER_GLOBAL_CAUSE",
                              "DEFER_SYS_GRP", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS")}
base["DEFER_ACTIVITY_OWNER"] = ev[0]["DEFER_ACTIVITY_OWNER"] or "Maintenance and Integrity"
for r in ev:
    r["DEFER_ACTIVITY_OWNER"] = base["DEFER_ACTIVITY_OWNER"]
db = DB({"deferments": [r for r in ev if r["PRODUCTION_DAY"] != "2025-09-17"]})  # 09-17 missing
zero = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=0, DEFERRED_COND_VOL_STM3=0)
msg = agent._write_block_reason(zero, mk_state(), db)
check("(a) zero volume blocked with expected full-day value", msg and msg.startswith("VOLUME CHECK")
      and "DEFERRED_GAS_VOL_SM3=2380000.0" in msg and "DEFERRED_COND_VOL_STM3=35.8" in msg)
ok = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2380000, DEFERRED_COND_VOL_STM3=35.8)
check("(a) correct volume allowed", agent._write_block_reason(ok, mk_state(), db) is None)
near = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2370000, DEFERRED_COND_VOL_STM3=35.7)
check("fix-005: 0.4% off now blocked (tolerance 0.01 / 0.01%)", agent._write_block_reason(near, mk_state(), db).startswith("VOLUME CHECK"))
tiny = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2380000.01, DEFERRED_COND_VOL_STM3=35.8)
check("fix-005: within tolerance allowed", agent._write_block_reason(tiny, mk_state(), db) is None)
off = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2222986.11, DEFERRED_COND_VOL_STM3=35.8)
check("(a) >1% off blocked", agent._write_block_reason(off, mk_state(), db).startswith("VOLUME CHECK"))
empty_cond = create(**base, PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2380000, DEFERRED_COND_VOL_STM3="")
check("(a) empty volume -> required-field block first", agent._write_block_reason(empty_cond, mk_state(), db).startswith("MISSING REQUIRED"))

# (b) new event (006-like): ALLOC 2,212,500 on day, 09:15 -> 15:15 (6 h down, 18 h up) -> R 2,950,000 -> 737,500
new = dict(EVENT_KEY="W_WELL-05_2025-11-11-1", WELL_NAME="WELL-05", PRODUCTION_DAY="2025-11-11", DEFER_CATEGORY_IND="Unscheduled",
           START_DATE="2025-11-11T09:15:00", END_DATE="2025-11-11T15:15:00", EVENT_TYPE="DOWN",
           DEFER_GLOBAL_CAUSE="Breakdown and breakdown maintenance (surface)", DEFER_SYS_GRP="Well", ASSET_TYPE="Well",
           DEFER_ACTIVITY_OWNER="Maintenance and Integrity")
db_b = DB({"daily_production": [{"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-11-10", "ALLOC_GAS_SM3": "2950000", "ALLOC_COND_M3": "44"},
                                {"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-11-11", "ALLOC_GAS_SM3": "2212500", "ALLOC_COND_M3": "33"}]})
wrong = create(**new, DEFERRED_GAS_VOL_SM3=553125, DEFERRED_COND_VOL_STM3=8.25)
msg = agent._write_block_reason(wrong, mk_state(task="Create a deferment for WELL-05 shut in from 09:15:00 to 15:15."), db_b)
check("(b) ALLOC-as-rate blocked, expected 737500", msg and "DEFERRED_GAS_VOL_SM3=737500.0" in msg and "DEFERRED_COND_VOL_STM3=11.0" in msg
      and "clean_day_stated_hours" in msg)
right = create(**new, DEFERRED_GAS_VOL_SM3=737500, DEFERRED_COND_VOL_STM3=11)
check("(b) correct derived volume allowed", agent._write_block_reason(right, mk_state(task="Create a deferment for WELL-05 starting 09:15."), db_b) is None)

# (c) skip: no rows and no ALLOC; CONSTRAINT skipped
check("fix-005: no data-derived estimate -> blocked (never guess)", "no data-derived volume_estimate" in agent._write_block_reason(wrong, mk_state(task="Create a deferment for WELL-05 starting 09:15."), DB({})))
cons = create(**dict(new, EVENT_TYPE="CONSTRAINT"), DEFERRED_GAS_VOL_SM3=1, DEFERRED_COND_VOL_STM3=1)
check("fix-005: CONSTRAINT validated by clean_day_diff", "DEFERRED_GAS_VOL_SM3=737500.0" in agent._write_block_reason(cons, mk_state(task="Create a deferment for WELL-05 starting 09:15."), db_b))
full_day_new = create(**dict(new, START_DATE="2025-11-11T00:00:00", END_DATE=""), DEFERRED_GAS_VOL_SM3=5, DEFERRED_COND_VOL_STM3=5)
check("fix-005: full-day new event validated (clean_day_diff)", agent._write_block_reason(full_day_new, mk_state(), db_b).startswith("VOLUME CHECK"))


# ---- order_by direction handling ----
class OrderWrfm:
    def __init__(self):
        self.sent = []

    def system(self): raise RuntimeError("offline")
    def wiki_tree(self): raise RuntimeError("offline")
    def data_schema(self): raise RuntimeError("offline")

    def dispatch(self, fn):
        self.sent.append(fn)
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None): pass


class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


def run(order_by):
    llm, w = FakeLLM([agent.NextStep(current_state="s", plan=["p"], function=Req_WrfmDataSelect(table_name="deferments", order_by=order_by)),
                      agent.NextStep(current_state="s", plan=["p"], function=Req_WrfmRespond(message="x", outcome="ok_answer"))]), OrderWrfm()
    agent.make_llm_client = lambda cfg: llm
    agent.run_agent(SimpleNamespace(get_wrfm_client=lambda t: w, log_llm=lambda **k: None),
                    SimpleNamespace(spec_id="zz_test_order", task_id="t", task_text="Summarise the WELL-01 deferments."),
                    llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
    return llm, w


llm, w = run("PRODUCTION_DAY")
check("bare order_by passed unchanged, no note", w.sent[0].order_by == "PRODUCTION_DAY" and "NOTE: order_by" not in str(llm.inputs[1]))
llm, w = run("DEFERRED_GAS_VOL_SM3 desc")
check("direction dropped, field kept", w.sent[0].order_by == "DEFERRED_GAS_VOL_SM3")
check("direction note", "NOTE: order_by direction is not supported" in str(llm.inputs[1]) and "ascending by DEFERRED_GAS_VOL_SM3" in str(llm.inputs[1]))
for t in Path(ROOT, "runs/traces").glob("zz_test_order_*.jsonl"):
    t.unlink()

# ---- Step 8 prompt ----
P = agent.SYSTEM_PROMPT
check("P-003 closing prefix", "Closing an event: Only when the task states the restart/end time explicitly. Run local_event_check first." in P)
check("P-003 required fields sentence", "Fill every required field. For a new event choose the closest approved labels" in P
      and "Leave fields empty that the task and data do not give" not in P)
check("P-003 group event rule", "For a group event (G_ key), also create rows for missing_wells" in P)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
