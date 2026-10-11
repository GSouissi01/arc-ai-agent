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


# 1) write rounding: Python round(value, 2) on the unrounded value
check("002 evidence: 46.8 x 14.5 / 24 -> 28.27", agent._round2(46.8 * 14.5 / 24) == 28.27)
est = agent._volume_estimate(DW(T), "WELL-02", "2025-09-12T09:30:00", None, "2025-09-12", "DOWN",
                             task_text="WELL-02 down from 09:30:00 to midnight")
print("  est", {k: est.get(k) for k in ("method", "clean_day", "R_cond", "downtime_h", "write_cond", "stated_end")})
check("volume_estimate R_cond 46.8 x 14.5 h -> write_cond 28.27",
      est.get("R_cond") == 46.8 and est.get("downtime_h") == 14.5 and est.get("write_cond") == 28.27)
ok = 0
for r in T["deferments"]:
    e = agent._volume_estimate(DW(T), r["WELL_NAME"], r["START_DATE"], r.get("END_DATE") or None, r["PRODUCTION_DAY"], r["EVENT_TYPE"])
    ok += e["write_gas"] == round(float(r["DEFERRED_GAS_VOL_SM3"]), 2) and e["write_cond"] == round(float(r["DEFERRED_COND_VOL_STM3"]), 2)
print(f"  stored rows matched: {ok}/{len(T['deferments'])}")
check("stored rows: 38/38", ok == 38 and len(T["deferments"]) == 38)

# 2) am/pm naive rule
def hours(text, day="2025-11-11"):
    w = agent._stated_window(text, day)
    return w and agent._window_hours_on_day(w, day)
w = agent._stated_window("from 07:00:00 to 12am", "2025-11-11")
check("07:00:00 -> 12am: end same day 12:00, 5 h", w[1].isoformat() == "2025-11-11T12:00:00" and hours("from 07:00:00 to 12am") == 5.0)
check("_stated_end 12am -> same day 12:00", agent._stated_end("from 07:00:00 to 12am", "2025-11-11T07:00:00") == "2025-11-11T12:00:00")
check("12pm end = 24:00", agent._stated_window("from 07:00 to 12pm", "2025-11-11")[1].isoformat() == "2025-11-12T00:00:00")
e12 = agent._stated_end("from 07:00 to 12pm", "2025-11-11T07:00:00")
check("12pm END_DATE serializes as next day 00:00:00, never 24:00:00", e12 == "2025-11-12T00:00:00" and "24:00" not in e12)
check("3pm = 15:00", agent._stated_window("from 07:00 to 3pm", "2025-11-11")[1].isoformat() == "2025-11-11T15:00:00")
check("7am = 07:00", agent._stated_window("from 7am to 09:30", "2025-11-11")[0].isoformat() == "2025-11-11T07:00:00")
check("midnight end stays 24:00", hours("from 22:00 to midnight") == 2.0)
check("midnight start stays 00:00", agent._stated_window("from midnight to 06:00", "2025-11-11")[0].isoformat() == "2025-11-11T00:00:00")
check("noon / midday = 12:00", hours("from 06:45:00 to midday") == 5.25 and hours("from 06:00 to noon") == 6.0)
check("END TIME guard sees am/pm", agent._has_explicit_time("restarted at 12am") and not agent._has_explicit_time("restarted today"))

# END_DATE guard on a new-event create
st = mk_state("Create a deferment for WELL-05 due to wing valve actuator fault from 07:00:00 to 12am.")
st.required = {}
st.loaded_docs.add("governance/raci.md")
def c(end, key="W_WELL-05_2025-11-11-1"):
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=v) for k, v in {
        "EVENT_KEY": key, "WELL_NAME": "WELL-05", "PRODUCTION_DAY": "2025-11-11", "START_DATE": "2025-11-11T07:00:00",
        "END_DATE": end, "EVENT_TYPE": "DOWN"}.items()])
b_bad = agent._write_block_reason(c("2025-11-12T00:00:00"), st, DW(T)) or ""
b_ok = agent._write_block_reason(c("2025-11-11T12:00:00"), st, DW(T)) or ""
print("  bad:", b_bad[:120]); print("  ok :", b_ok[:120])
check("END_DATE next-day 00:00 blocked -> 12:00", b_bad.startswith("END_DATE: the task states the window end; write END_DATE=2025-11-11T12:00:00"))
check("END_DATE 12:00 not blocked by END_DATE guard", not b_ok.startswith("END_DATE"))
b_ex = agent._write_block_reason(c("2025-11-12T00:00:00", key="G_NOVA7_2025-10-09-1"), st, DW(T)) or ""
check("existing event: END_DATE guard not applied", not b_ex.startswith("END_DATE"))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
