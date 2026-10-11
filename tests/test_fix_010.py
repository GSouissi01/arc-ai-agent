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

# ---- 010b: group-event flat-ALLOC missing well -> _volume(R, event span)
s10b = mk_state(T010); s10b.today = "2025-10-15"
c = agent._local_event_check(DWop(T), agent.LocalEventCheck(type="local_event_check", event_key=KEY), s10b)
w3e = [e for e in c.get("volume_estimate", []) if e["well"] == "WELL-03"]
print("  WELL-03 estimate", [{k: e.get(k) for k in ("method", "R_gas", "R_cond", "downtime_h", "write_gas", "write_cond")} for e in w3e])
check("010b 010 WELL-03 -> group_span_hours 2380000 x 12h -> 1190000.0 / 17.9",
      len(w3e) == 1 and w3e[0]["method"] == "group_span_hours" and w3e[0]["write_gas"] == 1190000.0 and w3e[0]["write_cond"] == 17.9)
check("010b WELL-03 still listed as missing well", any(m["well"] == "WELL-03" for m in c.get("missing_wells", [])))
peer = next(x for x in T["deferments"] if x["EVENT_KEY"] == KEY)
def crt(gas, cond):
    v = {k: peer[k] for k in ("EVENT_KEY", "DEFER_CATEGORY_IND", "START_DATE", "END_DATE", "EVENT_TYPE", "DEFER_GLOBAL_CAUSE",
                              "DEFER_SYS_GRP", "ASSET_TYPE", "COMMENTS")}
    v.update(WELL_NAME="WELL-03", PRODUCTION_DAY="2025-10-09", DEFER_ACTIVITY_OWNER="Process Engineering",
             DEFERRED_GAS_VOL_SM3=str(gas), DEFERRED_COND_VOL_STM3=str(cond))
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=x) for k, x in v.items()])
rows = [x for x in T["deferments"] if x["EVENT_KEY"] == KEY]
check("010b validator accepts 1190000.0 / 17.9", agent._volume_check(crt(1190000.0, 17.9), {fv.field_name: str(fv.value) for fv in crt(1190000.0, 17.9).field_values}, rows, DWop(T), s10b) is None)
v_old = agent._volume_check(crt(476000.0, 7.16), {fv.field_name: str(fv.value) for fv in crt(476000.0, 7.16).field_values}, rows, DWop(T), s10b) or ""
check("010b validator rejects old peer value 476000.0 -> expects 1190000.0", "DEFERRED_GAS_VOL_SM3=1190000.0" in v_old)
check("010b peers' own rows: no volume_mismatch (clean_day_diff unchanged)", c.get("volume_mismatch") == [])

print("FAILS:", fails)
sys.exit(1 if fails else 0)
