"""Offline test of fix-006 Steps 2/3, decision 1 (peer fraction) and the 004 experiment helper: no network."""
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


# stated window parsing
w = agent._stated_window("closed on 2025-12-15 due to wing valve actuator fault from 06:45:00 to midday.", "2025-12-15")
check("stated 06:45 -> midday = 5.25 h", w and agent._window_hours_on_day(w, "2025-12-15") == 5.25)
w = agent._stated_window("from 09:15:00 to 12am", "2025-11-11")
check("stated 09:15 -> 12am = 2.75 h (evaluator rule: 12am = 12:00)", agent._window_hours_on_day(w, "2025-11-11") == 2.75)
w = agent._stated_window("from 22:00 to 03:30", "2025-11-11")
check("crosses midnight: split per production day", agent._window_hours_on_day(w, "2025-11-11") == 2.0
      and agent._window_hours_on_day(w, "2025-11-12") == 3.5)
check("one time only -> no stated window", agent._stated_window("restart today at 16:45:00", "2025-11-11") is None)

# 006: R 2,950,000 x 5.25 h / 24 = 645,312.5
prod006 = {"daily_production": [{"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-12-14", "ALLOC_GAS_SM3": "2950000", "ALLOC_COND_M3": "44.2"},
                                {"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-12-15", "ALLOC_GAS_SM3": "2212500", "ALLOC_COND_M3": "33.15"}],
           "deferments": []}
task006 = "Create a deferment for WELL-05 closed on 2025-12-15 due to wing valve actuator fault from 06:45:00 to midday."
est = agent._volume_estimate(DW(prod006), "WELL-05", "2025-12-15T06:45:00", "2025-12-15T12:00:00", "2025-12-15", "DOWN", task_text=task006)
check("006 = clean_day_stated_hours 645312.5", est["method"] == "clean_day_stated_hours" and est["write_gas"] == 645312.5)
est = agent._volume_estimate(DW(prod006), "WELL-05", "2025-12-15T06:45:00", "2025-12-15T12:00:00", "2025-12-15", "DOWN")
check("006 without task text: span hours gives the same 645312.5", est["method"] == "clean_day_span_hours" and est["write_gas"] == 645312.5)

# 001 / 002 on the real snapshot
task001 = "Add yesterday's missing deferment record for the ongoing WELL-03 shut-in."
est = agent._volume_estimate(DW(T), "WELL-03", "2025-10-13T22:25:00", None, "2025-10-14", "DOWN", task_text=task001)
check("001 still 2380000.00 / 35.80", est["write_gas"] == 2380000.0 and est["write_cond"] == 35.8)
est = agent._volume_estimate(DW(T), "WELL-02", "2025-10-13T14:00:00", "2025-10-15T16:45:00", "2025-10-15", "DOWN",
                             task_text="Close the WELL-02 deferment; Ops confirmed restart today at 16:45:00.")
check("002-type closing day: span hours 00:00-16:45", est["method"] == "clean_day_span_hours" and abs(est["downtime_h"] - 16.75) < 1e-9)

# peer-fraction validator for the 010-type WELL-03 create
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


TASK010 = "Review the event related to export pressure control problems. Take actions to correct data if necessary"
peer = next(r for r in T["deferments"] if r["EVENT_KEY"] == "G_NOVA7_2025-10-09-1")
row = {k: peer[k] for k in ("EVENT_KEY", "DEFER_CATEGORY_IND", "START_DATE", "END_DATE", "EVENT_TYPE", "DEFER_GLOBAL_CAUSE",
                            "DEFER_SYS_GRP", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS")}
row.update(WELL_NAME="WELL-03", PRODUCTION_DAY="2025-10-09", DEFER_ACTIVITY_OWNER=peer["DEFER_ACTIVITY_OWNER"] or "Process Engineering")


def create(**f):
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=str(v)) for k, v in f.items()])


msg = agent._write_block_reason(create(**row, DEFERRED_GAS_VOL_SM3=0, DEFERRED_COND_VOL_STM3=0), mk_state(TASK010), DW(T))
check("010 WELL-03 create with 0 -> VOLUME CHECK expects peer fraction 476000/7.16", msg and "DEFERRED_GAS_VOL_SM3=476000.0" in msg
      and "DEFERRED_COND_VOL_STM3=7.16" in msg and "group_peer_fraction" in msg)
check("010 WELL-03 create with peer values -> allowed", agent._write_block_reason(
    create(**row, DEFERRED_GAS_VOL_SM3=476000.0, DEFERRED_COND_VOL_STM3=7.16), mk_state(TASK010), DW(T)) is None)

# forced event check key resolution
check("task well + date -> one event key", agent._task_event_key(
    DW(T), agent._TaskState(task_text="Review the 2025-10-09 export pressure control instability at WELL-01 and fix it.")) == "G_NOVA7_2025-10-09-1")
check("event key in text", agent._task_event_key(DW(T), agent._TaskState(task_text="Check event G_NOVA7_2025-09-13-1 please")) == "G_NOVA7_2025-09-13-1")
check("no key/date -> none", agent._task_event_key(DW(T), agent._TaskState(task_text="Close the open deferment record.")) is None)


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


llm, w = LLM([step(Req_WrfmDataSelect(table_name="deferments")),
              step(Req_WrfmRespond(message="consistent", outcome="ok_answer")),
              step(Req_WrfmRespond(message="consistent", outcome="ok_answer"), cs="WELL-03 not created because reason X")]), LoopW()
agent.make_llm_client = lambda cfg: llm
agent.run_agent(SimpleNamespace(get_wrfm_client=lambda t: w, log_llm=lambda **k: None),
                SimpleNamespace(spec_id="zz_test_006", task_id="t", task_text=TASK010),
                llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
check("single-event select -> auto [event_check] appended", "[event_check]" in str(llm.inputs[1]) and "group_peer_fraction" in str(llm.inputs[1]))
check("first ok_answer with unhandled missing -> EVENT CHECK block", "EVENT CHECK: unhandled items" in str(llm.inputs[2]) and "WELL-03" in str(llm.inputs[2]))
responds = [f for f in w.dispatched if isinstance(f, Req_WrfmRespond)]
check("second ok_answer with a stated reason -> allowed", len(responds) == 1)
check("refs: G_ event + flagged missing well cited",
      {("deferment_event", "G_NOVA7_2025-10-09-1"), ("well", "WELL-03")} <= {(r.type, r.id) for r in responds[0].ground_refs})
traces = list(Path(ROOT, "runs/traces").glob("zz_test_006_*.jsonl"))
check("override reason logged in trace", traces and "event_check_override" in traces[0].read_text(encoding="utf-8")
      and "reason X" in traces[0].read_text(encoding="utf-8"))
for t in traces:
    t.unlink()

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
