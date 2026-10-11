"""Offline test of fix-009 (event pick by COMMENTS, ASSET_TYPE precedent, review-task owner fill): no network."""
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


# ---- 009a: event selection by COMMENTS phrase first
TASKS = {  # snapshot-dated task texts (dates shifted to the snapshot)
    "001": "Add yesterday's missing deferment record for the ongoing WELL-03 shut-in.",
    "002": "Close the WELL-02 deferment; Ops confirmed restart today at 11:30:00.",
    "003": "Review the 2025-10-09 export pressure control instability at WELL-01 and set the responsible discipline based on the stated cause.",
    "004": "Summarise the largest deferment event. Give only deferred gas volume figure.",
    "005": "Close the WELL-02 deferment now; repair is done.",
    "006": "Create a deferment for WELL-05 closed on 2025-10-15 due to wing valve actuator fault from 08:45:00 to 12:00.",
    "007": "Close the open deferment record.",
    "008": "Create a deferment for WELL-04.",
    "009": "Close WELL-02 deferment as instrument repair is completed and well restarted.",
    "010": "Review the event related to export pressure control problems. Take actions to correct data if necessary",
}
W = DW(T)
for spec, text in TASKS.items():
    st = SimpleNamespace(task_text=text)
    old, new = agent._named_event_key(W, st), agent._task_event_key(W, st)
    if spec == "010":
        check("009a 010: COMMENTS phrase -> G_NOVA7_2025-10-09-1 (old: none)", old is None and new == "G_NOVA7_2025-10-09-1")
    else:
        check(f"009a {spec}: selection unchanged ({old})", old == new)
check("009a ambiguous phrase -> fallback (shut in: several events)", agent._comments_event_key(W, "well shut in for flowline valve leak") is None)
check("009a stop-word-only phrase ignored", agent._comments_event_key(W, "for the event of the") is None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
