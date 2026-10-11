"""Offline test of fix-010 (owner-guard narrowing, group span volume, numeric precision): no network."""
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


from ogchallenge_client.dtos import Req_WrfmDataUpdate, WrfmCondition


class DWop(DW):
    """Fake client that honours eq / ne / is_empty / in operators."""
    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        def ok(r, c):
            v = r.get(c.field_name)
            if c.operator == "eq":
                return str(v) == str(c.value)
            if c.operator == "ne":
                return str(v) != str(c.value)
            if c.operator == "is_empty":
                return v is None or str(v).strip() == ""
            if c.operator == "in":
                return str(v) in [x.strip() for x in str(c.value).split(",")]
            raise ValueError(c.operator)
        rows = [r for r in self.tables.get(table_name, []) if all(ok(r, c) for c in (where_clause or []))]
        return SimpleNamespace(rows=rows, next_offset=-1)


KEY = "G_NOVA7_2025-10-09-1"
T010 = "Review the event related to export pressure control problems. Take actions to correct data if necessary"
T003 = ("Review the 2025-10-09 export pressure control instability at WELL-01 and set the responsible discipline "
        "based on the stated cause.")
def upd(value, conds, extra=None):
    fv = [WrfmKeyValue(field_name="DEFER_ACTIVITY_OWNER", value=value)] + [WrfmKeyValue(field_name=k, value=v) for k, v in (extra or {}).items()]
    return Req_WrfmDataUpdate(table_name="deferments", field_values=fv,
                              where_clause=[WrfmCondition(field_name=f, operator=o, value=v) for f, o, v in conds])
def checked_state(text):
    s = mk_state(text); s.today = "2025-10-15"
    agent._local_event_check(DWop(T), agent.LocalEventCheck(type="local_event_check", event_key=KEY), s)
    return s

# ---- 010a: owner-guard narrowing predicates
s10 = checked_state(T010)
row = [("EVENT_KEY", "eq", KEY), ("WELL_NAME", "eq", "WELL-01"), ("PRODUCTION_DAY", "eq", "2025-10-09")]
check("010a 010_r1 shape (+ owner is_empty) -> allowed",
      agent._write_block_reason(upd("Process Engineering", row + [("DEFER_ACTIVITY_OWNER", "is_empty", None)]), s10, DWop(T)) is None)
check("010a 010_r1 shape (+ owner is_empty value '') -> allowed",
      agent._write_block_reason(upd("Process Engineering", row + [("DEFER_ACTIVITY_OWNER", "is_empty", "")]), s10, DWop(T)) is None)
check("010a EVENT_KEY + owner ne precedent -> allowed (only the 4 peers match)",
      agent._write_block_reason(upd("Process Engineering", [("EVENT_KEY", "eq", KEY), ("DEFER_ACTIVITY_OWNER", "ne", "Process Engineering")]), s10, DWop(T)) is None)
w3 = dict(next(x for x in T["deferments"] if x["EVENT_KEY"] == KEY), WELL_NAME="WELL-03", DEFER_ACTIVITY_OWNER="Surface Production")
T2 = dict(T, deferments=T["deferments"] + [w3])
check("010a owner ne matching a non-peer row (new WELL-03) -> blocked",
      (agent._write_block_reason(upd("Process Engineering", [("EVENT_KEY", "eq", KEY), ("DEFER_ACTIVITY_OWNER", "ne", "Process Engineering")]), s10, DWop(T2)) or "").startswith("OWNER FILL BLOCKED"))
check("010a broadening: WELL_NAME in -> blocked",
      agent._write_block_reason(upd("Process Engineering", [("EVENT_KEY", "eq", KEY), ("WELL_NAME", "in", "WELL-01,WELL-02")]), s10, DWop(T)) is not None)
check("010a broadening: EVENT_KEY not eq -> blocked",
      agent._write_block_reason(upd("Process Engineering", [("EVENT_KEY", "contains", "G_NOVA7")]), s10, DWop(T)) is not None)
check("010a owner is_not_empty predicate -> blocked",
      agent._write_block_reason(upd("Process Engineering", row + [("DEFER_ACTIVITY_OWNER", "is_not_empty", None)]), s10, DWop(T)) is not None)
check("010a non-owner field change -> blocked",
      agent._write_block_reason(upd("Process Engineering", row + [("DEFER_ACTIVITY_OWNER", "is_empty", None)], {"COMMENTS": "x"}), s10, DWop(T)) is not None)
check("010a no EVENT_KEY predicate -> blocked",
      agent._write_block_reason(upd("Process Engineering", [("WELL_NAME", "eq", "WELL-01"), ("DEFER_ACTIVITY_OWNER", "is_empty", None)]), s10, DWop(T)) is not None)
s3 = checked_state(T003)
b3 = agent._write_block_reason(upd("Process Engineering", row + [("DEFER_ACTIVITY_OWNER", "is_empty", None)]), s3, DWop(T)) or ""
check("010a 003 path untouched: OWNER BLOCKED not stated (clarify)", not s3.owner_fill and b3.startswith("OWNER BLOCKED") and agent._clarify_guard(b3) is not None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
