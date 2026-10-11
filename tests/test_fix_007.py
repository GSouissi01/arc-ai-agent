"""Offline test of fix-007 (blocked write -> clarification, event-check scope, write scope, COMMENTS convention, peer mismatch): no network."""
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


TASK010 = "Review the event related to export pressure control problems. Take actions to correct data if necessary"
peer = next(r for r in T["deferments"] if r["EVENT_KEY"] == "G_NOVA7_2025-10-09-1")
row = {k: peer[k] for k in ("EVENT_KEY", "DEFER_CATEGORY_IND", "START_DATE", "END_DATE", "EVENT_TYPE", "DEFER_GLOBAL_CAUSE",
                            "DEFER_SYS_GRP", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS")}
row.update(WELL_NAME="WELL-03", PRODUCTION_DAY="2025-10-09", DEFER_ACTIVITY_OWNER=peer["DEFER_ACTIVITY_OWNER"] or "Process Engineering")



from ogchallenge_client.dtos import Req_WrfmDataUpdate, WrfmCondition
T010 = "Review the event related to export pressure control problems. Take actions to correct data if necessary"
T003 = "Review the 2025-10-09 export pressure control instability at WELL-01 and set the responsible discipline based on the stated cause."
check("scope: 010 review/correct -> review task", agent._review_task(T010))
check("scope: 003 set-owner -> not review task", not agent._review_task(T003))
check("scope: 001 add missing -> not review task", not agent._review_task("Add yesterday's missing deferment record for the ongoing WELL-03 shut-in."))
check("scope: close -> not review task", not agent._review_task("Check and close the WELL-02 deferment at 16:45:00."))
check("scope: reconcile -> review task", agent._review_task("Reconcile the WELL-04 deferment volumes."))
check("clarify guard: owner not stated", agent._clarify_guard("OWNER BLOCKED: DEFER_ACTIVITY_OWNER is the accountable discipline; it is not stated in the task or x")[0] == "OWNER BLOCKED")
check("clarify guard: owner label error is not clarification", agent._clarify_guard("OWNER BLOCKED: 'X' is not an approved DEFER_ACTIVITY_OWNER label.") is None)
check("clarify guard: end time", agent._clarify_guard("END TIME BLOCKED: the task states no restart/end time.")[0] == "END TIME BLOCKED")
check("clarify guard: no volume_estimate", agent._clarify_guard("VOLUME CHECK: no data-derived volume_estimate for this row (x).") is not None)
check("clarify guard: other blocks ignored", agent._clarify_guard("EVENT CONSISTENCY: copy from existing rows") is None)
st = agent._TaskState(task_text=T003)
st.clarify_blocks.append(("OWNER BLOCKED", "the accountable DEFER_ACTIVITY_OWNER"))
ok = Req_WrfmRespond(message="done", outcome="ok_answer")
check("blocked write, nothing written -> ok_answer blocked", (agent._blocked_write_block(ok, st) or "").startswith("BLOCKED WRITE: OWNER BLOCKED"))
check("clarification outcome passes", agent._blocked_write_block(Req_WrfmRespond(message="?", outcome="none_clarification_needed"), st) is None)
st.write_ok = True
check("a write succeeded -> no blocked-write guard", agent._blocked_write_block(ok, st) is None)

# write scope: intent per task text (the 10 dev specs)
TEXTS = {
    "001": ("Add yesterday's missing deferment record for the ongoing WELL-03 shut-in.", "create"),
    "002": ("Close the WELL-02 deferment; Ops confirmed restart today at 17:45:00.", "close"),
    "003": (T003, "set owner"),
    "004": ("Summarise the largest deferment event. Give only deferred gas volume figure.", "unknown"),
    "005": ("Close the WELL-02 deferment now; repair is done.", "close"),
    "006": ("Create a deferment for WELL-05 closed on 2025-11-06 due to wing valve actuator fault from 06:15:00 to 12:00.", "create"),
    "007": ("Close the open deferment record.", "close"),
    "008": ("Create a deferment for WELL-04.", "create"),
    "009": ("Close WELL-02 deferment as instrument repair is completed and well restarted.", "close"),
    "010": (T010, "review"),
}
for spec, (text, intent) in TEXTS.items():
    check(f"intent {spec} = {intent}", agent._write_intent(text)[0] == intent)
s003 = mk_state(T003)
crt = Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name="WELL_NAME", value="WELL-03")])
check("003: create -> WRITE SCOPE", (agent._write_block_reason(crt, s003) or "").startswith("WRITE SCOPE: task asks set owner; create not allowed"))
s004 = mk_state(TEXTS["004"][0])
s004.trace_path = Path(ROOT, "runs/traces/zz_test_007_scope.jsonl")
check("unknown intent: update allowed by scope", not (agent._write_block_reason(Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="COMMENTS", value="x")], where_clause=[]), s004) or "").startswith("WRITE SCOPE"))
check("unknown intent logged in trace", "write_scope" in s004.trace_path.read_text(encoding="utf-8") and "intent unknown" in s004.trace_path.read_text(encoding="utf-8"))
came = "WELL-02 came back online at 14:00"
check("'came back online' -> close intent, create + update", agent._write_intent(came) == ("close", {"create", "update"}))
for text in ["WELL-02 back on line at 14:00", "Production resumed at 09:00", "Gas export restored at noon", "WELL-01 returned to service at 10:00",
             "WELL-03 brought back at 08:00", "Shut-in ended at 06:00", "Repair finished, WELL-02 restarted"]:
    check(f"close intent: {text!r}", agent._write_intent(text)[0] == "close")
check("006 'closed on' is still create", agent._write_intent(TEXTS["006"][0])[0] == "create")
check("create task: update still WRITE SCOPE", (agent._write_block_reason(Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="COMMENTS", value="x")], where_clause=[]), mk_state(TEXTS["008"][0])) or "").startswith("WRITE SCOPE: task asks create; update not allowed"))
check("001: update -> WRITE SCOPE", (agent._write_block_reason(Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="COMMENTS", value="x")], where_clause=[]), mk_state(TEXTS["001"][0])) or "").startswith("WRITE SCOPE: task asks create; update not allowed"))
check("002: create allowed by scope", not (agent._write_block_reason(crt, mk_state(TEXTS["002"][0])) or "").startswith("WRITE SCOPE"))

# COMMENTS convention
check("build_comment 006", agent.build_comment("wing valve actuator fault", "partial", "DOWN") == "Wing valve actuator fault deferment; prorated by downtime within the day.")
check("build_comment full day -> None", agent.build_comment("x", "full", "DOWN") is None)
check("build_comment CONSTRAINT -> None", agent.build_comment("x", "partial", "CONSTRAINT") is None)
T006 = TEXTS["006"][0]
def c006(key="W_WELL-05_2025-11-06-1", start="2025-11-06T06:15:00", end="2025-11-06T12:00:00", et="DOWN"):
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=v) for k, v in {
        "EVENT_KEY": key, "WELL_NAME": "WELL-05", "PRODUCTION_DAY": "2025-11-06", "START_DATE": start, "END_DATE": end,
        "EVENT_TYPE": et, "COMMENTS": "wing valve actuator fault"}.items()])
check("006 new event partial DOWN -> built comment", agent._convention_comment(c006(), mk_state(T006), DW(T)) == "Wing valve actuator fault deferment; prorated by downtime within the day.")
check("full-day row -> no build", agent._convention_comment(c006(start="2025-11-06T00:00:00", end=""), mk_state(T006), DW(T)) is None)
check("CONSTRAINT -> no build", agent._convention_comment(c006(et="CONSTRAINT"), mk_state(T006), DW(T)) is None)
check("existing event -> no build (copy)", agent._convention_comment(c006(key="G_NOVA7_2025-10-09-1"), mk_state(T006), DW(T)) is None)
check("task gives a comment -> no build", agent._convention_comment(c006(), mk_state(T006 + " Comment: valve fault."), DW(T)) is None)
check("no cause wording -> no build", agent._convention_comment(c006(), mk_state("Create a deferment for WELL-05."), DW(T)) is None)

# peer-fraction mismatch: after the 010-type WELL-03 create, no mismatch; a 0 row is flagged with the peer value
peer = next(r for r in T["deferments"] if r["EVENT_KEY"] == "G_NOVA7_2025-10-09-1")
def with_w3(gas, cond):
    rows = [dict(r) for r in T["deferments"]] + [dict(peer, WELL_NAME="WELL-03", DEFERRED_GAS_VOL_SM3=gas, DEFERRED_COND_VOL_STM3=cond)]
    return DW(dict(T, deferments=rows))
ev = agent.LocalEventCheck(type="local_event_check", event_key="G_NOVA7_2025-10-09-1")
r = agent._local_event_check(with_w3(1190000.0, 17.9), ev, agent._TaskState(task_text=T010, today="2025-10-15"))
check("after WELL-03 create: no volume_mismatch", r["volume_mismatch"] == [] and r["missing"] == [])
r = agent._local_event_check(with_w3(0.0, 0.0), ev, agent._TaskState(task_text=T010, today="2025-10-15"))
check("WELL-03 stored 0 -> mismatch expects span value 1190000/17.9 (fix-010b)",
      [(m["well"], m["expected_gas"], m["expected_cond"], m["method"]) for m in r["volume_mismatch"]] == [("WELL-03", 1190000.0, 17.9, "group_span_hours")])


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



def step(fn, cs="s"):
    return agent.NextStep(current_state=cs, plan=["p"], function=fn)


row_w3 = {k: peer[k] for k in peer}
row_w3.update(WELL_NAME="WELL-03", DEFERRED_GAS_VOL_SM3=476000.0, DEFERRED_COND_VOL_STM3=7.16, DEFER_ACTIVITY_OWNER="Process Engineering")
upd = Req_WrfmDataUpdate(table_name="deferments",
                         field_values=[WrfmKeyValue(field_name="DEFER_ACTIVITY_OWNER", value="Process Engineering")],
                         where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value="G_NOVA7_2025-10-09-1")])
crt3 = Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=str(v)) for k, v in dict(row_w3).items()])
llm, w = LLM([step(crt3), step(upd),
              step(Req_WrfmRespond(message="Owner set", outcome="ok_answer")),
              step(Req_WrfmRespond(message="Which accountable discipline?", outcome="none_clarification_needed"))]), LoopW()
agent.make_llm_client = lambda cfg: llm
agent.run_agent(SimpleNamespace(get_wrfm_client=lambda t: w, log_llm=lambda **k: None),
                SimpleNamespace(spec_id="zz_test_007", task_id="t", task_text=T003),
                llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
first = str(llm.inputs[0])
check("bootstrap [event_check] labelled INFO ONLY", "[event_check] INFO ONLY" in first)
check("003 create of missing well -> WRITE SCOPE", "WRITE SCOPE: task asks set owner; create not allowed" in str(llm.inputs[1]))
check("owner update blocked (not stated)", "OWNER BLOCKED" in str(llm.inputs[2]))
check("ok_answer after blocked write -> BLOCKED WRITE", "BLOCKED WRITE: OWNER BLOCKED" in str(llm.inputs[3]))
check("no write dispatched", not [f for f in w.dispatched if isinstance(f, (Req_WrfmDataCreate, Req_WrfmDataUpdate))])
responds = [f for f in w.dispatched if isinstance(f, Req_WrfmRespond)]
check("ends none_clarification_needed", len(responds) == 1 and responds[0].outcome == "none_clarification_needed")
check("clarification cites the event", ("deferment_event", "G_NOVA7_2025-10-09-1") in {(r.type, r.id) for r in responds[0].ground_refs})
for t in Path(ROOT, "runs/traces").glob("zz_test_007_*.jsonl"):
    t.unlink()

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
