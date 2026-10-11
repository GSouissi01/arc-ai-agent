"""Offline test of fix-004 Steps 1,2,3,5,6,7: no network."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
from ogchallenge_client.dtos import (
    Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmDataUpdate, Req_WrfmRespond,
    Resp_WrfmRespond, WrfmCondition, WrfmGroundRef, WrfmKeyValue,
)
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


SCHEMA = json.loads(Path(ROOT, "runs/wiki/data_schema.json").read_text(encoding="utf-8"))
RACI = agent._parse_raci(Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8"))


def kv(**f):
    return [WrfmKeyValue(field_name=k, value=v) for k, v in f.items()]


def mk_state(task="Review and correct the deferment event data.", role="Production Programmer"):
    s = agent._TaskState(role=role, raci=RACI, task_text=task)
    for table, info in SCHEMA["tables"].items():
        fields = info.get("fields") or {}
        s.required[table] = [f for f, spec in fields.items() if spec.get("required")]
        s.enums[table] = {f: spec["enum"] for f, spec in fields.items() if spec.get("enum")}
    return s


FULL = dict(EVENT_KEY="W_WELL-05_2025-11-11-1", WELL_NAME="WELL-05", PRODUCTION_DAY="2025-11-11",
            DEFERRED_COND_VOL_STM3="11.0", DEFERRED_GAS_VOL_SM3="737500", DEFER_CATEGORY_IND="Unscheduled",
            START_DATE="2025-11-11T09:15:00", END_DATE="2025-11-11T15:15:00", EVENT_TYPE="DOWN", DEFER_GLOBAL_CAUSE="Breakdown and breakdown maintenance (surface)",
            DEFER_SYS_GRP="Well", ASSET_TYPE="Well", DEFER_ACTIVITY_OWNER="Maintenance and Integrity")


PROD = [{"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-11-09", "ALLOC_GAS_SM3": "2950000", "ALLOC_COND_M3": "44"},
        {"WELL_NAME": "WELL-05", "REPORT_DAY": "2025-11-11", "ALLOC_GAS_SM3": "2212500", "ALLOC_COND_M3": "33"}]


class EventDB:
    """Fake WRFM data for guard lookups and group checks."""
    def __init__(self, tables):
        tables = dict(tables)
        tables.setdefault("daily_production", PROD)
        self.tables, self.calls = tables, []

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        self.calls.append((table_name, [(c.field_name, c.value) for c in (where_clause or [])]))
        rows = [r for r in self.tables.get(table_name, [])
                if all(str(r.get(c.field_name)) == str(c.value) for c in (where_clause or []))]
        return SimpleNamespace(rows=rows[offset:offset + 1000], next_offset=-1)


# ---- Step 2: required fields ----
s = mk_state()
check("schema required parsed", "DEFER_ACTIVITY_OWNER" in s.required["deferments"] and "END_DATE" not in s.required["deferments"]
      and "COMMENTS" not in s.required["deferments"])
partial = {k: v for k, v in FULL.items() if k not in ("DEFER_SYS_GRP", "ASSET_TYPE")}
msg = agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=kv(**partial)), s, EventDB({}))
check("missing required -> blocked + listed", msg and msg.startswith("MISSING REQUIRED FIELDS") and "DEFER_SYS_GRP" in msg and "ASSET_TYPE" in msg)
empty = dict(FULL, DEFER_ACTIVITY_OWNER="")
msg = agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=kv(**empty)), s, EventDB({}))
check("empty required value -> blocked", msg and "DEFER_ACTIVITY_OWNER" in msg)
check("full new-event create allowed", agent._write_block_reason(
    Req_WrfmDataCreate(table_name="deferments", field_values=kv(**FULL)), mk_state(), EventDB({})) is None)

# ---- Step 3: owner scope ----
bad_label = dict(FULL, DEFER_ACTIVITY_OWNER="Ops Team")
msg = agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=kv(**bad_label)), mk_state(), EventDB({}))
check("new event, unapproved owner -> LABEL block with approved list", msg and msg.startswith("LABEL:") and "Well Services" in msg)
existing_db = EventDB({"deferments": [dict(FULL, PRODUCTION_DAY="2025-11-10", DEFER_ACTIVITY_OWNER="Well Services")]})
msg = agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=kv(**FULL)), mk_state(), existing_db)
check("existing event, different owner -> blocked with expected", msg and "existing event" in msg and "Well Services" in msg)
same = dict(FULL, DEFER_ACTIVITY_OWNER="Well Services")
check("existing event, copied owner -> allowed", agent._write_block_reason(
    Req_WrfmDataCreate(table_name="deferments", field_values=kv(**same)), mk_state(), existing_db) is None)
upd = Req_WrfmDataUpdate(table_name="deferments", field_values=kv(DEFER_ACTIVITY_OWNER="Process Engineering"),
                         where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value="EVX")])
check("update owner inferred -> blocked", agent._write_block_reason(upd, mk_state(), EventDB({})).startswith("OWNER BLOCKED"))
check("update owner in task -> allowed", agent._write_block_reason(upd, mk_state(task="set owner to Process Engineering"), EventDB({})) is None)

# ---- Step 5: END_DATE time guard ----
close = Req_WrfmDataUpdate(table_name="deferments", field_values=kv(END_DATE="2026-02-09T00:00:00"),
                           where_clause=[WrfmCondition(field_name="WELL_NAME", operator="eq", value="WELL-02")])
msg = agent._write_block_reason(close, mk_state(task="Close WELL-02 deferment as repair is completed and well restarted."), EventDB({}))
check("close without time -> END TIME BLOCKED", msg and msg.startswith("END TIME BLOCKED"))
for t in ["restart today at 16:45:00", "restarted at 7:05", "back online at noon", "Close the deferment; downtime from 09:15 to 12am", "restart 3 PM", "Close the deferment at midnight"]:
    check(f"explicit time ok: {t!r}", agent._write_block_reason(close, mk_state(task=t), EventDB({})) is None)
check("date only is not a time", agent._has_explicit_time("restarted on 2026-02-09") is False)
new_with_end = dict(FULL)
check("new event create with END_DATE not time-guarded", agent._write_block_reason(
    Req_WrfmDataCreate(table_name="deferments", field_values=kv(**new_with_end)), mk_state(task="Create a new deferment for WELL-01."), EventDB({})) is None)
exist_end = dict(same, END_DATE="2025-11-12T00:00:00")
check("existing event create with a different END_DATE, no time -> blocked", agent._write_block_reason(
    Req_WrfmDataCreate(table_name="deferments", field_values=kv(**exist_end)), mk_state(task="close it"), existing_db).startswith(("END TIME BLOCKED", "EVENT CONSISTENCY")))
check("authority still first (OS close)", agent._write_block_reason(close, mk_state(role="Operations Supervisor"), EventDB({})).startswith("AUTHORITY BLOCKED"))

# ---- Step 6: ref enricher ----
s = mk_state(task="Review WELL-01 event")
s.loaded_docs.update({"governance/raci.md"})
s.used_tables.update({"deferments", "daily_production"})
s.seen_ids.update({"G_NOVA7_2026-01-17-1", "WELL-03"})
resp = Req_WrfmRespond(message="m", outcome="ok_answer", ground_refs=[
    WrfmGroundRef(type="wiki", id="governance/raci.md"), WrfmGroundRef(type="wiki", id="made/up.md"),
    WrfmGroundRef(type="well", id="WELL-03"), WrfmGroundRef(type="well", id="WELL-01"), WrfmGroundRef(type="well", id="WELL-09"),
    WrfmGroundRef(type="deferment_event", id="G_NOVA7_2026-01-17-1"), WrfmGroundRef(type="deferment_event", id="W_FAKE-1"),
    WrfmGroundRef(type="table", id="deferments"), WrfmGroundRef(type="table", id="well_info"),
])
new, dropped = agent._enrich_refs(resp, s)
ids = [(r.type, r.id) for r in new.ground_refs]
check("refs: unseen dropped", {d["id"] for d in dropped} == {"made/up.md", "WELL-09", "W_FAKE-1", "well_info"})
check("refs: task-text well kept", ("well", "WELL-01") in ids)
check("refs: missing used table added with label", ("table", "daily_production") in ids
      and next(r for r in new.ground_refs if r.id == "daily_production").label == "daily_production")
check("refs: no duplicate table, no wiki added", ids.count(("table", "deferments")) == 1
      and sum(1 for t, _ in ids if t == "wiki") == 1)

# ---- Step 7: group-event completeness ----
EVK = "G_NOVA7_2026-03-01-1"
start, end = "2026-03-01T08:00:00", "2026-03-01T20:00:00"
def drow(well, key=EVK, day="2026-03-01"):
    return dict(EVENT_KEY=key, WELL_NAME=well, PRODUCTION_DAY=day, START_DATE=start, END_DATE=end,
                DEFERRED_GAS_VOL_SM3="100", DEFERRED_COND_VOL_STM3="1", DEFER_ACTIVITY_OWNER="Process Engineering")
db = EventDB({
    "deferments": [drow("W1"), drow("W2"), drow("W5", key="W_W5_2026-03-01-1")],
    "well_info": [{"WELL_NAME": w} for w in ["W1", "W2", "W3", "W4", "W5", "W6"]],
    "well_status_history": [{"WELL_NAME": "W4", "STATUS_DESC": "CLOSED_LT", "STATUS_DATE": "2026-01-01", "STATUS_END_DATE": ""}],
    "daily_production": [{"WELL_NAME": w, "REPORT_DAY": "2026-03-01", "ALLOC_GAS_SM3": v}
                         for w, v in [("W3", "1500000"), ("W4", "0"), ("W5", "800000"), ("W6", "0")]]
                        + [{"WELL_NAME": "W3", "REPORT_DAY": "2026-02-28", "ALLOC_GAS_SM3": "2400000", "ALLOC_COND_M3": "30"}],
})
st = agent._TaskState(today="2026-03-05")
res = agent._local_event_check(db, agent.LocalEventCheck(type="local_event_check", event_key=EVK), st)
mw = {m["well"] for m in res.get("missing_wells", [])}
check("group: producing well without row -> missing_well", mw == {"W3"})
check("group: closed (W4), other-deferred (W5), not producing (W6) excluded", not mw & {"W4", "W5", "W6"})
check("group: missing includes W3 day", {"well": "W3", "day": "2026-03-01"} in res["missing"])
check("group: evidence present", res["missing_wells"][0]["evidence"]["alloc_gas_on_event_days"] == {"2026-03-01": 1500000.0})
res = agent._local_event_check(EventDB({"deferments": [drow("W1", key="W_W1_2026-03-01-1")]}),
                               agent.LocalEventCheck(type="local_event_check", event_key="W_W1_2026-03-01-1"), st)
check("non-group event: no missing_wells key", "missing_wells" not in res)


# ---- Step 1: traces + loop integration ----
class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class LoopWrfm(EventDB):
    def __init__(self):
        super().__init__({"deferments": [drow("W1"), drow("W2")]})
        self.dispatched, self.responded = [], []

    def system(self):
        return SimpleNamespace(model_dump_json=lambda: json.dumps({"role": "Production Programmer", "today": "2026-03-05"}))

    def wiki_tree(self, path=""): return SimpleNamespace(tree=".\n`-- governance/\n    `-- raci.md\n")
    def data_schema(self): return SimpleNamespace(model_dump_json=lambda: json.dumps(SCHEMA))

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WikiLoad):
            return SimpleNamespace(content=Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8"))
        if isinstance(fn, Req_WrfmDataSelect):
            rows = self.tables.get(fn.table_name, [])
            return SimpleNamespace(rows=rows, model_dump_json=lambda exclude_none=True: json.dumps({"rows": rows}))
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None): self.responded.append(outcome)


def step(fn):
    return agent.NextStep(current_state="st", plan=["p"], function=fn)


llm, wrfm = FakeLLM([
    step(Req_WrfmDataSelect(table_name="deferments")),
    step(Req_WrfmDataCreate(table_name="deferments", field_values=kv(EVENT_KEY="X", WELL_NAME="W1"))),
    step(Req_WrfmRespond(message="done", outcome="ok_answer", ground_refs=[WrfmGroundRef(type="well", id="W9")])),
]), LoopWrfm()
api = SimpleNamespace(get_wrfm_client=lambda t: wrfm, log_llm=lambda **k: None)
agent.make_llm_client = lambda cfg: llm
before = set(Path(ROOT, "runs/traces").glob("zz_test_*.jsonl")) if Path(ROOT, "runs/traces").exists() else set()
agent.run_agent(api, SimpleNamespace(spec_id="zz_test_spec", task_id="t", task_text="Review the event."),
                llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
traces = sorted(set(Path(ROOT, "runs/traces").glob("zz_test_spec_*.jsonl")) - before)
check("trace file created", len(traces) == 1)
lines = [json.loads(l) for l in traces[0].read_text(encoding="utf-8").splitlines()] if traces else []
steps = [l for l in lines if "step" in l]
check("trace: one line per step with full args/result", len(steps) == 3 and steps[0]["args"]["table_name"] == "deferments"
      and '"rows"' in steps[0]["result"] and "tokens" in steps[0] and "elapsed_ms" in steps[0])
check("trace: block message recorded", steps[1]["block"] and steps[1]["block"].startswith("MISSING REQUIRED FIELDS"))
check("trace: dropped ref logged", steps[2]["dropped_refs"] == [{"type": "well", "id": "W9"}])
check("trace: final respond is last line", "final_respond" in lines[-1]
      and {"type": "table", "id": "deferments", "label": "deferments"} in lines[-1]["final_respond"]["ground_refs"])
sent = [f for f in wrfm.dispatched if isinstance(f, Req_WrfmRespond)][0]
check("respond dispatched with enriched refs (table + auto event refs)", [(r.type, r.id) for r in sent.ground_refs] == [("table", "deferments"), ("deferment_event", "G_NOVA7_2026-03-01-1"), ("deferment_event", "X"), ("well", "W1")])
for t in traces:
    t.unlink()  # remove fake-test trace

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
