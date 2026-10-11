"""Offline test of fix-005 volume_estimate (H1') on real snapshot data via the agent code path: no network."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
from ogchallenge_client.dtos import Req_WrfmDataCreate, WrfmKeyValue
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


DATA = {t: json.loads(Path(ROOT, f"runs/data/{t}.json").read_text(encoding="utf-8"))
        for t in ("deferments", "daily_production", "well_status_history", "well_info")}
SCHEMA = json.loads(Path(ROOT, "runs/wiki/data_schema.json").read_text(encoding="utf-8"))
RACI = agent._parse_raci(Path(ROOT, "runs/wiki/governance/raci.md").read_text(encoding="utf-8"))


class DataWrfm:
    def __init__(self, tables):
        self.tables = tables

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        rows = [r for r in self.tables.get(table_name, [])
                if all(str(r.get(c.field_name)) == str(c.value) for c in (where_clause or []))]
        return SimpleNamespace(rows=rows[offset:offset + limit], next_offset=-1 if offset + limit >= len(rows) else offset + limit)


w = DataWrfm(DATA)

# 1) H1' on all 38 stored rows via _volume_estimate
ok, near, methods, misses = 0, 0, {}, []
for r in DATA["deferments"]:
    est = agent._volume_estimate(w, r["WELL_NAME"], r["START_DATE"], r.get("END_DATE") or None, r["PRODUCTION_DAY"], r["EVENT_TYPE"])
    methods[est["method"]] = methods.get(est["method"], 0) + 1
    good = (est["method"] is not None
            and est["write_gas"] == round(float(r["DEFERRED_GAS_VOL_SM3"]), 2)
            and est["write_cond"] == round(float(r["DEFERRED_COND_VOL_STM3"]), 2))
    ok += good
    near += (est["method"] is not None and abs(est["deferred_gas"] - float(r["DEFERRED_GAS_VOL_SM3"])) <= 0.0100001
             and abs(est["deferred_cond"] - float(r["DEFERRED_COND_VOL_STM3"])) <= 0.0100001)
    if not good:
        misses.append((r["EVENT_KEY"], r["PRODUCTION_DAY"], est))
print(f"  code path: {ok}/{len(DATA['deferments'])} exact to 2 decimals; methods {methods}")
for m in misses:
    print("  MISS", m)
print(f"  exact {ok}/38, within 0.01 {near}/38")
check("H1' via code path: 38/38 within 0.01 (validator tolerance)", near == len(DATA["deferments"]) == 38)
check("write values == stored on 37/38 rows (Python round; 1 known source double-rounding row)", ok == 37)

# 2) 001: missing day 2025-10-14 of ongoing W_WELL-03_2025-10-13-1 -> 2,380,000.00 / 35.80 exactly
est = agent._volume_estimate(w, "WELL-03", "2025-10-13T22:25:00", None, "2025-10-14", "DOWN")
check("001 missing day = clean_day_span_hours 2380000.00 / 35.80",
      est["method"] == "clean_day_span_hours" and round(est["deferred_gas"], 2) == 2380000.00 and round(est["deferred_cond"], 2) == 35.80
      and est["clean_day"] == "2025-10-12")
st = agent._TaskState(today="2025-10-15")
res = agent._local_event_check(w, agent.LocalEventCheck(type="local_event_check", event_key="W_WELL-03_2025-10-13-1"), st)
ve = res["volume_estimate"]
check("001 local_event_check: missing [10-14] with volume_estimate 2380000.0",
      [m["day"] for m in res["missing"]] == ["2025-10-14"] and len(ve) == 1 and round(ve[0]["deferred_gas"], 2) == 2380000.0)

# 3) other paths
est = agent._volume_estimate(w, "WELL-03", "2025-10-13T22:25:00", "2025-10-20T06:00:00", "2025-10-20", "DOWN")
check("no daily_production for day, DOWN -> clean_day_span_hours (6h of 2,380,000)",
      est["method"] == "clean_day_span_hours" and abs(est["deferred_gas"] - 2380000 * 6 / 24) < 1e-6)
est = agent._volume_estimate(w, "WELL-03", "2025-10-13T22:25:00", None, "2025-10-20", "CONSTRAINT")
check("no daily_production for day, CONSTRAINT -> no method, clarification", est["method"] is None and "clarification_needed" in est)
no_clean = DataWrfm({"deferments": [], "daily_production": [{"WELL_NAME": "X", "REPORT_DAY": "2025-10-14", "ALLOC_GAS_SM3": "0", "ALLOC_COND_M3": "0"}]})
rows = [{"WELL_NAME": "X", "PRODUCTION_DAY": "2025-10-14", "START_DATE": "2025-10-14T00:00:00", "END_DATE": "",
         "DEFERRED_GAS_VOL_SM3": "1000", "DEFERRED_COND_VOL_STM3": "1"}]
est = agent._volume_estimate(no_clean, "X", "2025-10-14T00:00:00", None, "2025-10-15", "DOWN", rows)
check("no clean day, DOWN -> best_R_fallback", est["method"] == "best_R_fallback" and est["deferred_gas"] == 1000.0)
est = agent._volume_estimate(no_clean, "X", "2025-10-14T00:00:00", None, "2025-10-15", "CONSTRAINT", rows)
check("no clean day, CONSTRAINT -> clarification", est["method"] is None)
grp = agent._local_event_check(w, agent.LocalEventCheck(type="local_event_check", event_key="G_NOVA7_2025-10-09-1"), st)
ve3 = [e for e in grp["volume_estimate"] if e["well"] == "WELL-03"]
check("fix-006: flat-ALLOC facility well -> missing via group_peer_fraction ~476000/7.16 (gas fraction for cond, 2 dp)",
      [m["well"] for m in grp["missing_wells"]] == ["WELL-03"] and len(ve3) == 1 and ve3[0]["method"] == "group_peer_fraction"
      and ve3[0]["write_gas"] == 476000.0 and ve3[0]["write_cond"] == 7.16 and ve3[0]["cond_decimals"] == 2 and "unaffected_wells" not in grp)
# 010-like: drop WELL-05's row from the CONSTRAINT group event -> WELL-05 is missing with its own clean-day estimate
data3 = dict(DATA, deferments=[r for r in DATA["deferments"] if not (r["EVENT_KEY"] == "G_NOVA7_2025-10-09-1" and r["WELL_NAME"] == "WELL-05")])
g3 = agent._local_event_check(DataWrfm(data3), agent.LocalEventCheck(type="local_event_check", event_key="G_NOVA7_2025-10-09-1"), st)
e5 = [e for e in g3["volume_estimate"] if e["well"] == "WELL-05"]
check("010-like: removed well becomes missing_well with exact CONSTRAINT estimate 590000/8.9",
      sorted(m["well"] for m in g3["missing_wells"]) == ["WELL-03", "WELL-05"] and len(e5) == 1 and e5[0]["method"] == "clean_day_diff"
      and round(e5[0]["deferred_gas"], 2) == 590000.0 and round(e5[0]["deferred_cond"], 2) == 8.9
      and {"well": "WELL-05", "day": "2025-10-09"} in g3["missing"])

# 4) validator uses volume_estimate (incl. CONSTRAINT)
def mk_state():
    s = agent._TaskState(role="Production Programmer", raci=RACI, task_text="add the missing day")
    for table, info in SCHEMA["tables"].items():
        fields = info.get("fields") or {}
        s.required[table] = [f for f, spec in fields.items() if spec.get("required")]
        s.enums[table] = {f: spec["enum"] for f, spec in fields.items() if spec.get("enum")}
    return s


src = next(r for r in DATA["deferments"] if r["EVENT_KEY"] == "W_WELL-03_2025-10-13-1")
row = {k: src[k] for k in ("EVENT_KEY", "WELL_NAME", "DEFER_CATEGORY_IND", "START_DATE", "EVENT_TYPE", "DEFER_GLOBAL_CAUSE",
                           "DEFER_SYS_GRP", "ASSET_TYPE", "DEFER_ACTIVITY_OWNER", "COMMENTS")}
row["DEFER_ACTIVITY_OWNER"] = row["DEFER_ACTIVITY_OWNER"] or "Maintenance and Integrity"
data2 = dict(DATA, deferments=[dict(r, DEFER_ACTIVITY_OWNER=r["DEFER_ACTIVITY_OWNER"] or "Maintenance and Integrity")
                               if r["EVENT_KEY"] == "W_WELL-03_2025-10-13-1" else r for r in DATA["deferments"]])
w2 = DataWrfm(data2)


def create(**f):
    return Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=str(v)) for k, v in f.items()])


msg = agent._write_block_reason(create(**row, PRODUCTION_DAY="2025-10-14", DEFERRED_GAS_VOL_SM3=2380000.52, DEFERRED_COND_VOL_STM3=35.77), mk_state(), w2)
check("001 validator: 2380000.52 blocked, expects 2380000.0/35.8 clean_day_diff", msg and "DEFERRED_GAS_VOL_SM3=2380000.0" in msg
      and "DEFERRED_COND_VOL_STM3=35.8" in msg and "clean_day_span_hours" in msg)
check("001 validator: exact value allowed", agent._write_block_reason(
    create(**row, PRODUCTION_DAY="2025-10-14", DEFERRED_GAS_VOL_SM3=2380000.00, DEFERRED_COND_VOL_STM3=35.80), mk_state(), w2) is None)
cons = dict(row, EVENT_KEY="W_WELL-03_2025-10-21-1", START_DATE="2025-10-21T08:00:00", EVENT_TYPE="CONSTRAINT")
msg = agent._write_block_reason(create(**cons, PRODUCTION_DAY="2025-10-21", DEFERRED_GAS_VOL_SM3=1000, DEFERRED_COND_VOL_STM3=1), mk_state(), w2)
check("CONSTRAINT without day data -> VOLUME CHECK clarification", msg and "no data-derived volume_estimate" in msg)

print("ALL PASS" if not fails else f"{fails} FAILED")

# ---- fix-005 addendum: volume_mismatch + per-day missing coverage ----
fails2 = 0
r0 = agent._local_event_check(w, agent.LocalEventCheck(type="local_event_check", event_key="W_WELL-03_2025-09-14-1"), st)
print(("PASS " if r0["volume_mismatch"] == [] else "FAIL ") + "real event: no volume_mismatch")
fails2 += r0["volume_mismatch"] != []
bad = [dict(r, DEFERRED_GAS_VOL_SM3="1000") if (r["EVENT_KEY"] == "G_NOVA7_2025-09-13-1" and r["WELL_NAME"] == "WELL-02") else r
       for r in DATA["deferments"]]
r1 = agent._local_event_check(DataWrfm(dict(DATA, deferments=bad)), agent.LocalEventCheck(type="local_event_check", event_key="G_NOVA7_2025-09-13-1"), st)
vm = r1["volume_mismatch"]
ok = len(vm) == 1 and vm[0]["well"] == "WELL-02" and vm[0]["stored_gas"] == 1000.0 and vm[0]["expected_gas"] == 2583333.33
print(("PASS " if ok else "FAIL ") + "tampered row -> volume_mismatch with expected 2583333.33")
fails2 += not ok
gone = [r for r in DATA["deferments"] if not (r["EVENT_KEY"] == "W_WELL-03_2025-09-14-1" and r["PRODUCTION_DAY"] == "2025-09-16")]
r2 = agent._local_event_check(DataWrfm(dict(DATA, deferments=gone)), agent.LocalEventCheck(type="local_event_check", event_key="W_WELL-03_2025-09-14-1"), st)
ok = [m["day"] for m in r2["missing"]] == ["2025-09-16"] and len(r2["volume_estimate"]) == 1 and r2["volume_estimate"][0]["method"] is not None
print(("PASS " if ok else "FAIL ") + f"event-well missing day across span -> missing + estimate ({r2['volume_estimate'][0].get('method')}, {round(r2['volume_estimate'][0].get('deferred_gas') or 0, 2)})")
fails2 += not ok
print("ADDENDUM ALL PASS" if not fails2 else f"ADDENDUM {fails2} FAILED")
sys.exit(fails + fails2)
