"""Offline test of fix-012 (EXPERIMENTAL raw event figure for answer-only responses): no network."""
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


from ogchallenge_client.dtos import WrfmGroundRef
T004 = "Summarise the largest deferment event. Give only deferred gas volume figure."
KEY = "W_WELL-03_2025-09-14-1"
def resp(msg, key=KEY, outcome="ok_answer"):
    refs = [WrfmGroundRef(type="table", id="deferments", label="deferments")]
    if key:
        refs.append(WrfmGroundRef(type="deferment_event", id=key, label=key))
    return Req_WrfmRespond(message=msg, outcome=outcome, ground_refs=refs)
st = SimpleNamespace(task_text=T004)
out = agent._raw_event_figure(resp("14717986.11"), st, DW(T))
print("  004 ->", out and out.message)
check("012 004: 14717986.11 -> 14717986.111111112", out is not None and out.message == "14717986.111111112")
check("012 cond figure on same event -> raw cond sum", (agent._raw_event_figure(resp("221.39"), st, DW(T)) or resp("x")).message not in ("x", "221.39"))
check("012 number not equal to the event's stored sum -> unchanged", agent._raw_event_figure(resp("14717986"), st, DW(T)) is None)
check("012 no deferment_event ref -> unchanged", agent._raw_event_figure(resp("14717986.11", key=None), st, DW(T)) is None)
check("012 not an answer-only task -> unchanged",
      agent._raw_event_figure(resp("14717986.11"), SimpleNamespace(task_text="Summarise the largest deferment event."), DW(T)) is None)
check("012 non-numeric message -> unchanged", agent._raw_event_figure(resp("14,717,986.11"), st, DW(T)) is None)
check("012 clarification outcome -> unchanged", agent._raw_event_figure(resp("14717986.11", outcome="none_clarification_needed"), st, DW(T)) is None)
g = "G_NOVA7_2025-10-09-1"  # CONSTRAINT rows (clean_day_diff): R x span not derivable -> fallback
gsum = round(sum(float(r["DEFERRED_GAS_VOL_SM3"]) for r in T["deferments"] if r["EVENT_KEY"] == g), 2)
check("012 CONSTRAINT event (no span method) -> fallback unchanged", agent._raw_event_figure(resp(repr(gsum), key=g), st, DW(T)) is None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
