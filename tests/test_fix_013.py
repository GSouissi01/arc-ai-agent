"""Offline test of fix-013a (EXPERIMENTAL largest event = highest single-day gas row): no network."""
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
def resp(msg, key="W_WELL-03_2025-09-14-1", outcome="ok_answer"):
    return Req_WrfmRespond(message=msg, outcome=outcome, ground_refs=[WrfmGroundRef(type="table", id="deferments", label="deferments"),
                                                                        WrfmGroundRef(type="deferment_event", id=key, label=key)])
st = SimpleNamespace(task_text=T004)
out = agent._largest_event_answer(resp("14717986.11"), st, DW(T))
print("  004 ->", out and (out.message, [(r.type, r.id) for r in out.ground_refs]))
check("013a 004: -> 5683333.33 (W_WELL-02_2025-10-13-1)", out is not None and out.message == "5683333.33")
check("013a 004: event ref replaced by the winner",
      [r.id for r in out.ground_refs if r.type == "deferment_event"] == ["W_WELL-02_2025-10-13-1"] and any(r.type == "table" for r in out.ground_refs))
check("013a not answer-only -> unchanged", agent._largest_event_answer(resp("14717986.11"), SimpleNamespace(task_text="Summarise the largest deferment event."), DW(T)) is None)
check("013a no 'largest ... event' -> unchanged", agent._largest_event_answer(resp("14717986.11"), SimpleNamespace(task_text="Give only the deferred gas volume figure of W_WELL-03_2025-09-14-1."), DW(T)) is None)
check("013a condensate figure -> unchanged", agent._largest_event_answer(resp("221.39"), SimpleNamespace(task_text="Largest deferment event: give only the condensate figure."), DW(T)) is None)
check("013a non-numeric / clarification -> unchanged", agent._largest_event_answer(resp("x"), st, DW(T)) is None
      and agent._largest_event_answer(resp("1", outcome="none_clarification_needed"), st, DW(T)) is None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
