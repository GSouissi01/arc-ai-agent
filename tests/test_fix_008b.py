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


import inspect
from datetime import datetime as dt

# _volume = round(R * seconds / 86400, 2), (R * s) first
src = inspect.getsource(agent._volume)
check("_volume source: rate * (end_dt - start_dt).total_seconds() / 86400",
      "round(rate * (end_dt - start_dt).total_seconds() / 86400, 2)" in src and "/ 86400)" not in src.replace(", 2)", ""))
s, e = dt(2025, 9, 11, 2, 20), dt(2025, 9, 11, 21, 10)
check("order lock: (R*s)/86400 -> 36.73 (R*(s/86400) would give 36.72)",
      agent._volume(46.8, s, e) == 36.73 and round(46.8 * ((e - s).total_seconds() / 86400), 2) == 36.72)

# miss row via the agent path
r = next(x for x in T["deferments"] if x["EVENT_KEY"] == "G_NOVA7_2025-09-11-1" and x["WELL_NAME"] == "WELL-02")
est = agent._volume_estimate(DW(T), r["WELL_NAME"], r["START_DATE"], r.get("END_DATE") or None, r["PRODUCTION_DAY"], r["EVENT_TYPE"])
print("  miss row est", {k: est.get(k) for k in ("method", "R_cond", "write_gas", "write_cond")}, "stored", r["DEFERRED_COND_VOL_STM3"])
check("G_NOVA7_2025-09-11-1 WELL-02 cond -> 36.73", est["write_cond"] == 36.73)

# 14.5 h stated window
est = agent._volume_estimate(DW(T), "WELL-02", "2025-09-12T09:30:00", None, "2025-09-12", "DOWN",
                             task_text="WELL-02 down from 09:30:00 to midnight")
check("WELL-02 14.5 h stated -> 28.27", est["method"] == "clean_day_stated_hours" and est["write_cond"] == 28.27)
est = agent._volume_estimate(DW(T), "WELL-02", "2025-09-12T09:30:00", None, "2025-09-12", "DOWN")
check("WELL-02 14.5 h span (ongoing) -> 28.27", est["method"] == "clean_day_span_hours" and est["write_cond"] == 28.27)

# best_R_fallback routed through _volume
rows = [x for x in T["deferments"] if x["EVENT_KEY"] == "G_NOVA7_2025-09-11-1"]
fb = agent._volume_estimate(DW({"deferments": [], "daily_production": []}), "WELL-02", r["START_DATE"], r["END_DATE"],
                            r["PRODUCTION_DAY"], "DOWN", rows)
check("best_R_fallback uses _volume (2 dp)", fb["method"] == "best_R_fallback" and fb["write_cond"] == round(fb["write_cond"], 2))

# all 38 stored rows
ok = sum(agent._volume_estimate(DW(T), x["WELL_NAME"], x["START_DATE"], x.get("END_DATE") or None, x["PRODUCTION_DAY"], x["EVENT_TYPE"])
         .get("write_gas") == round(float(x["DEFERRED_GAS_VOL_SM3"]), 2) and
         agent._volume_estimate(DW(T), x["WELL_NAME"], x["START_DATE"], x.get("END_DATE") or None, x["PRODUCTION_DAY"], x["EVENT_TYPE"])
         .get("write_cond") == round(float(x["DEFERRED_COND_VOL_STM3"]), 2) for x in T["deferments"])
print(f"  stored rows matched: {ok}/{len(T['deferments'])}")
check("stored rows 38/38", ok == 38 == len(T["deferments"]))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
