"""Offline test of fix-005 Steps 1-6: no network."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
from ogchallenge_client.dtos import (
    Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmDataUpdate, Req_WrfmRespond, Resp_WrfmRespond,
    WrfmCondition, WrfmGroundRef, WrfmKeyValue,
)
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


SCHEMA = json.loads(Path(ROOT, "runs/wiki/data_schema.json").read_text(encoding="utf-8"))
RACI_MD = Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8")
RACI = agent._parse_raci(RACI_MD)
REAL = json.loads(Path(ROOT, "runs/data/deferments.json").read_text(encoding="utf-8"))


def mk_state(task="Review and correct the deferment event data.", role="Production Programmer"):
    s = agent._TaskState(role=role, raci=RACI, task_text=task)
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


# ---- Step 2: authority note + respond guard ----
s = mk_state(task="Close the WELL-02 deferment now; repair is done.", role="Operations Supervisor")
note = agent._authority_note(s)
check("authority note lists NO authority for close", "close deferment rows: consulted -> NO authority" in note)
clar = Req_WrfmRespond(message="Need restart time", outcome="none_clarification_needed")
msg = agent._respond_block_reason(clar, s)
check("OS close + clarification -> AUTHORITY respond block", msg and msg.startswith("AUTHORITY:") and "close deferment rows" in msg)
check("OS close + denied_security -> allowed", agent._respond_block_reason(
    Req_WrfmRespond(message="no", outcome="denied_security"), s) is None)
check("PP close + clarification -> allowed", agent._respond_block_reason(clar, mk_state(task="Close the WELL-02 deferment")) is None)
check("OS review task (no write keyword) -> allowed", agent._respond_block_reason(
    Req_WrfmRespond(message="ok", outcome="ok_answer"), mk_state(task="Review the event", role="Operations Supervisor")) is None)
check("owner verb+noun detected", agent._respond_block_reason(
    Req_WrfmRespond(message="?", outcome="none_clarification_needed"),
    mk_state(task="set the responsible discipline", role="Operations Supervisor")).startswith("AUTHORITY:"))
check("PP owner set -> allowed", agent._respond_block_reason(
    Req_WrfmRespond(message="?", outcome="none_clarification_needed"), mk_state(task="set the responsible discipline")) is None)

# ---- Step 3: LABEL validator ----
new = dict(EVENT_KEY="W_WELL-05_2025-11-11-1", WELL_NAME="WELL-05", PRODUCTION_DAY="2025-11-11", DEFER_CATEGORY_IND="Unscheduled",
           START_DATE="2025-11-11T09:15:00", END_DATE="2025-11-12T00:00:00", EVENT_TYPE="DOWN",
           DEFER_GLOBAL_CAUSE="wing valve actuator fault", DEFER_SYS_GRP="Well", ASSET_TYPE="Well",
           DEFER_ACTIVITY_OWNER="Maintenance and Integrity", DEFERRED_GAS_VOL_SM3=1, DEFERRED_COND_VOL_STM3=1)
msg = agent._write_block_reason(create(**new), mk_state(task="Create a deferment for WELL-01 from 09:15 to 12am."), DB({}))
check("free-text cause -> LABEL block with approved list", msg and msg.startswith("LABEL: 'wing valve actuator fault'")
      and "Breakdown and breakdown maintenance (surface)" in msg)
upd = Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="DEFER_SYS_GRP", value="valves")])
check("update with bad sys grp -> LABEL", agent._write_block_reason(upd, mk_state(), DB({})).startswith("LABEL:"))

# ---- Step 4 + 5: consistency + END copy exception (real closed event, one day removed) ----
ev = [dict(r) for r in REAL if r["EVENT_KEY"] == "W_WELL-03_2025-09-14-1"]
for r in ev:
    r["DEFER_ACTIVITY_OWNER"] = r["DEFER_ACTIVITY_OWNER"] or "Maintenance and Integrity"
db = DB({"deferments": [r for r in ev if r["PRODUCTION_DAY"] != "2025-09-17"]})
src = ev[0]
row = {k: src[k] for k in ("EVENT_KEY", "WELL_NAME", "DEFER_CATEGORY_IND", "START_DATE", "END_DATE", "EVENT_TYPE",
                           "DEFER_GLOBAL_CAUSE", "DEFER_SYS_GRP", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS")}
row.update(PRODUCTION_DAY="2025-09-17", DEFERRED_GAS_VOL_SM3=2380000, DEFERRED_COND_VOL_STM3=35.8)
check("full copy of closed event (END copied, no time in task) -> allowed",
      agent._write_block_reason(create(**row), mk_state(task="Review the event and fix missing data"), db) is None)
no_comment = dict(row, COMMENTS="")
msg = agent._write_block_reason(create(**no_comment), mk_state(task="Review"), db)
check("missing COMMENTS -> EVENT CONSISTENCY with expected", msg and msg.startswith("EVENT CONSISTENCY") and "'COMMENTS'" in msg)
other_comment = dict(row, COMMENTS=ev[-1]["COMMENTS"])
check("COMMENTS from another row of same event -> allowed", agent._write_block_reason(create(**other_comment), mk_state(task="Add the missing deferment row for this event."), db) is None)
wrong_start = dict(row, START_DATE="2025-09-17T00:00:00")
check("wrong START_DATE -> EVENT CONSISTENCY", "START_DATE" in agent._write_block_reason(create(**wrong_start), mk_state(task="Add the missing deferment row for this event."), db))
new_end = dict(row, END_DATE="2025-09-21T10:00:00")
msg = agent._write_block_reason(create(**new_end), mk_state(task="Add the missing deferment row for this event."), db)
check("different END_DATE on closed event -> EVENT CONSISTENCY", msg and "END_DATE" in msg)
check("value stated in task -> allowed change", agent._write_block_reason(
    create(**dict(row, COMMENTS="repair delayed by spares")), mk_state(task="add comment: repair delayed by spares"), db) is None)
# open event: END guard still blocks invented END on create
open_rows = [dict(r, END_DATE="") for r in ev if r["PRODUCTION_DAY"] != "2025-09-17"]
msg = agent._write_block_reason(create(**dict(row, END_DATE="2025-09-21T00:00:00")), mk_state(task="add missing day"), DB({"deferments": open_rows}))
check("open event + invented END on create -> END TIME BLOCKED", msg and msg.startswith("END TIME BLOCKED"))
upd_end = Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="END_DATE", value="2025-09-21T00:00:00")])
check("update END without time still blocked", agent._write_block_reason(upd_end, mk_state(task="close it"), DB({})).startswith(("END TIME", "CLOSE NO TIME")))

# ---- Step 6: best R + 001 re-check ----
rates = agent._rate_per_well(ev)
check("best R from full-day rows = 2,380,000 exactly", rates["WELL-03"]["gas"] == 2380000.0 and rates["WELL-03"]["cond"] == 35.8)
amp = [dict(r) for r in ev if r["PRODUCTION_DAY"] in ("2025-09-14", "2025-09-15")]
amp[0]["DEFERRED_GAS_VOL_SM3"] = 1066041.72  # start-day row with tiny rounding noise
check("rows within 0.1% of best R are median-combined (spec)", abs(agent._rate_per_well(amp)["WELL-03"]["gas"] - 2380000.06) < 0.01)

# ---- Step 1: refs ----
s = mk_state(task="Summarise the largest deferment event.")
agent._collect_ids({"groups": [{"group": "W_WELL-03_2025-10-20-1", "value": 1.0}]}, s)
resp = Req_WrfmRespond(message="1", outcome="ok_answer", ground_refs=[WrfmGroundRef(type="deferment_event", id="W_WELL-03_2025-10-20-1")])
new_resp, dropped = agent._enrich_refs(resp, s)
check("local_aggregate group id counts as seen", not dropped and ("deferment_event", "W_WELL-03_2025-10-20-1") in [(r.type, r.id) for r in new_resp.ground_refs])
s = mk_state(task="Review the 2025-11-06 event")
s.event_targets.add("G_NOVA7_2025-10-10-1")
new_resp, _ = agent._enrich_refs(Req_WrfmRespond(message="?", outcome="none_clarification_needed"), s)
check("event target auto-cited", ("deferment_event", "G_NOVA7_2025-10-10-1") in [(r.type, r.id) for r in new_resp.ground_refs])
s = mk_state()
s.selected_keys.update({"A", "B"})
new_resp, _ = agent._enrich_refs(Req_WrfmRespond(message="?", outcome="ok_answer"), s)
check("two selected events -> no auto event ref", not [r for r in new_resp.ground_refs if r.type == "deferment_event"])


# ---- loop: OS close answered with clarification is blocked, then denied_security goes through ----
class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class LoopWrfm(DB):
    def __init__(self, role):
        super().__init__({"deferments": [r for r in ev]})
        self.role, self.dispatched = role, []

    def system(self): return SimpleNamespace(model_dump_json=lambda: json.dumps({"role": self.role, "today": "2025-09-25"}))
    def wiki_tree(self, path=""): return SimpleNamespace(tree=".\n`-- governance/\n    `-- raci.md\n")
    def data_schema(self): return SimpleNamespace(model_dump_json=lambda: json.dumps(SCHEMA))

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WikiLoad):
            return SimpleNamespace(content=RACI_MD)
        if isinstance(fn, Req_WrfmDataSelect):
            rows = self.tables["deferments"]
            return SimpleNamespace(rows=rows, model_dump_json=lambda exclude_none=True: json.dumps({"rows": rows}))
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None): pass


def step(fn):
    return agent.NextStep(current_state="s", plan=["p"], function=fn)


llm, w = FakeLLM([step(Req_WrfmDataSelect(table_name="deferments")),
                  step(Req_WrfmRespond(message="When did it restart?", outcome="none_clarification_needed")),
                  step(Req_WrfmRespond(message="Not authorised", outcome="denied_security"))]), LoopWrfm("Operations Supervisor")
agent.make_llm_client = lambda cfg: llm
agent.run_agent(SimpleNamespace(get_wrfm_client=lambda t: w, log_llm=lambda **k: None),
                SimpleNamespace(spec_id="zz_test_005", task_id="t", task_text="Close the WELL-03 deferment now."),
                llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
responds = [f for f in w.dispatched if isinstance(f, Req_WrfmRespond)]
check("loop: authority note in context", "[authority]" in str(llm.inputs[0]) and "NO authority" in str(llm.inputs[0]))
check("loop: clarification blocked, denied_security dispatched once", [r.outcome for r in responds] == ["denied_security"])
check("loop: block message shown to model", "AUTHORITY: role lacks authority" in str(llm.inputs[2]))
check("loop: single selected event auto-cited", ("deferment_event", "W_WELL-03_2025-09-14-1") in [(r.type, r.id) for r in responds[0].ground_refs])
for t in Path(ROOT, "runs/traces").glob("zz_test_005_*.jsonl"):
    t.unlink()

# ---- prompt ----
P = agent.SYSTEM_PROMPT
check("prompt rule 0", "0. Check authority before anything else" in P)
check("prompt label mapping", "Never ask for clarification only because a free-text cause" in P)
check("prompt owner conflict resolved", "For a new event the owner is required" in P)
check("prompt copy incl COMMENTS/END_DATE", "END_DATE (if the event is closed)" in P and "activity owner and COMMENTS" in P)
check("prompt cite event always", "Always cite the deferment event you were asked about" in P)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
