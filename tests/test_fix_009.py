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

# ---- 009c: review-task owner fill (set-owner path untouched)
from ogchallenge_client.dtos import Req_WrfmDataUpdate, WrfmCondition
KEY = "G_NOVA7_2025-10-09-1"
def upd(value, where, extra=None):
    fv = [WrfmKeyValue(field_name="DEFER_ACTIVITY_OWNER", value=value)] + [WrfmKeyValue(field_name=k, value=v) for k, v in (extra or {}).items()]
    return Req_WrfmDataUpdate(table_name="deferments", field_values=fv,
                              where_clause=[WrfmCondition(field_name=k, operator="eq", value=v) for k, v in where.items()])
def check_event(state, tables=T):
    return agent._local_event_check(DW(tables), agent.LocalEventCheck(type="local_event_check", event_key=KEY), state)

# 003 replay: set-owner intent -> no owner_fill, update blocked as before -> clarification
s3 = mk_state(TASKS["003"]); s3.today = "2025-10-15"
c3 = check_event(s3)
check("009c 003: event check has no owner_fill (set-owner intent)", "owner_fill" not in c3 and not s3.owner_fill)
b3 = agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY}), s3, DW(T)) or ""
check("009c 003: owner update -> OWNER BLOCKED not stated (clarify guard)", b3.startswith("OWNER BLOCKED") and agent._clarify_guard(b3) is not None)
s3.clarify_blocks.append(agent._clarify_guard(b3))
r3 = agent._blocked_write_block(Req_WrfmRespond(message="done", outcome="ok_answer", ground_refs=[]), s3) or ""
check("009c 003: ok_answer -> BLOCKED WRITE -> clarification", r3.startswith("BLOCKED WRITE: OWNER BLOCKED"))

# 010: review intent -> owner_fill Process Engineering on the 4 peer rows
s10 = mk_state(TASKS["010"]); s10.today = "2025-10-15"
c10 = check_event(s10)
fill = c10.get("owner_fill") or {}
print("  owner_fill", {k: fill.get(k) for k in ("value", "rows", "source_events")})
check("009c 010: owner_fill = Process Engineering from W_WELL-02_2025-10-06-1",
      fill.get("value") == "Process Engineering" and fill.get("source_events") == ["W_WELL-02_2025-10-06-1"])
check("009c 010: 4 peer rows WELL-01/02/04/05", sorted(r["well"] for r in fill.get("rows", [])) == ["WELL-01", "WELL-02", "WELL-04", "WELL-05"])
ok_rows = 0
for r in fill.get("rows", []):
    b = agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY, "WELL_NAME": r["well"], "PRODUCTION_DAY": r["day"]}), s10, DW(T))
    ok_rows += b is None
check("009c 010: 4 per-row owner updates allowed", ok_rows == 4)
check("009c 010: EVENT_KEY-wide update allowed while only the 4 peers match",
      agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY}), s10, DW(T)) is None)
check("009c 010: other value -> OWNER FILL BLOCKED",
      (agent._write_block_reason(upd("Surface Production", {"EVENT_KEY": KEY}), s10, DW(T)) or "").startswith("OWNER FILL BLOCKED"))
check("009c 010: extra field -> blocked",
      agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY}, {"COMMENTS": "x"}), s10, DW(T)) is not None)
w3 = dict(next(x for x in T["deferments"] if x["EVENT_KEY"] == KEY), WELL_NAME="WELL-03", DEFER_ACTIVITY_OWNER="Process Engineering")
T2 = dict(T, deferments=T["deferments"] + [w3])
check("009c 010: update also matching a non-peer row (new WELL-03) -> blocked",
      (agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY}), s10, DW(T2)) or "").startswith("OWNER FILL BLOCKED"))
w3e = dict(w3, DEFER_ACTIVITY_OWNER="")
check("009c 010: non-peer row with empty owner -> blocked",
      agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY, "WELL_NAME": "WELL-03"}), s10,
                                DW(dict(T, deferments=T["deferments"] + [w3e]))) is not None)
s10b = mk_state(TASKS["010"]); s10b.today = "2025-10-15"
check("009c 010: no event check yet -> no fill -> blocked",
      agent._write_block_reason(upd("Process Engineering", {"EVENT_KEY": KEY}), s10b, DW(T)) is not None)
eb = agent._event_check_block(Req_WrfmRespond(message="ok", outcome="ok_answer", ground_refs=[]), s10, "x") or ""
check("009c 010: ok_answer before owner updates -> EVENT CHECK lists empty_owner", "empty_owner G_NOVA7_2025-10-09-1 WELL-01" in eb)
s10.updated_pairs |= {(r["well"], r["day"]) for r in fill["rows"]}
s10.written_pairs |= {(m["well"], m["day"]) for m in c10.get("missing", [])}
s10.event_check_blocks = 0
check("009c 010: after the 4 updates (+ missing create) no unhandled items",
      agent._event_check_block(Req_WrfmRespond(message="ok", outcome="ok_answer", ground_refs=[]), s10, "x") is None)
# no unique precedent -> no fill
T3 = dict(T, deferments=T["deferments"] + [dict(next(x for x in T["deferments"] if x["EVENT_KEY"] == "W_WELL-02_2025-10-06-1"),
                                                  EVENT_KEY="W_WELL-04_2025-10-02-1", DEFER_ACTIVITY_OWNER="Surface Production")])
s10c = mk_state(TASKS["010"]); s10c.today = "2025-10-15"
check("009c conflicting precedent -> no owner_fill", "owner_fill" not in check_event(s10c, T3))

# ---- 009b: new W_ event ASSET_TYPE = strict majority of the well's own W_ event rows
check("009b precedent WELL-05 -> Well (W_ rows: Well 3, Facility 1)", agent._asset_type_precedent(DW(T), "WELL-05") == "Well")
check("009b precedent all wells -> Well", all(agent._asset_type_precedent(DW(T), w) == "Well" for w in ("WELL-01", "WELL-02", "WELL-03", "WELL-04")))
tie = [dict(r, EVENT_KEY=f"W_WELL-09_2025-10-0{i}-1", WELL_NAME="WELL-09", ASSET_TYPE=a)
       for i, (r, a) in enumerate(zip(T["deferments"][:2], ("Well", "Facility")), 1)]
TT = dict(T, deferments=T["deferments"] + tie)
check("009b conflicting precedent (tie) -> None (fallback)", agent._asset_type_precedent(DW(TT), "WELL-09") is None)
check("009b no rows -> None (fallback)", agent._asset_type_precedent(DW(T), "WELL-77") is None)
T006 = TASKS["006"]
s6 = mk_state(T006); s6.required = {}
est = agent._volume_estimate(DW(T), "WELL-05", "2025-10-15T08:45:00", "2025-10-15T12:00:00", "2025-10-15", "DOWN", task_text=T006)
def c6(asset, well="WELL-05", key="W_WELL-05_2025-10-15-1", tables=T):
    vals = {"EVENT_KEY": key, "WELL_NAME": well, "PRODUCTION_DAY": "2025-10-15", "START_DATE": "2025-10-15T08:45:00",
            "END_DATE": "2025-10-15T12:00:00", "EVENT_TYPE": "DOWN", "DEFERRED_GAS_VOL_SM3": str(est.get("write_gas")),
            "DEFERRED_COND_VOL_STM3": str(est.get("write_cond"))}
    if asset:
        vals["ASSET_TYPE"] = asset
    return agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=v) for k, v in vals.items()]), s6, DW(tables)) or ""
check("009b 006: Equipment -> ASSET_TYPE block expecting Well", c6("Equipment").startswith("ASSET_TYPE: new individual-well event on WELL-05: write ASSET_TYPE=Well"))
check("009b 006: Well -> no ASSET_TYPE block", not c6("Well").startswith("ASSET_TYPE"))
check("009b existing event row -> not applied", not c6("Equipment", key="W_WELL-05_2025-10-12-1").startswith("ASSET_TYPE"))
check("009b G_ event -> not applied", not c6("Equipment", key="G_NOVA7_2025-10-15-1").startswith("ASSET_TYPE"))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
