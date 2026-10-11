"""Offline test of fix-011 (close task without a stated time blocks every write): no network."""
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

T007 = "Close the open deferment record."
T009 = "Close WELL-02 deferment as instrument repair is completed and well restarted."
T002 = "Close the WELL-02 deferment; Ops confirmed restart today at 11:30:00."
T005 = "Close the WELL-02 deferment now; repair is done."
T006 = "Create a deferment for WELL-05 closed on 2025-10-15 due to wing valve actuator fault from 08:45:00 to 12:00."
def st(text, role="Production Programmer"):
    s = mk_state(text); s.role = role; s.today = "2025-10-15"
    return s
def end_upd(key, end):
    return Req_WrfmDataUpdate(table_name="deferments", field_values=[WrfmKeyValue(field_name="END_DATE", value=end)],
                              where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value=key)])
w3 = next(r for r in T["deferments"] if r["EVENT_KEY"] == "W_WELL-03_2025-10-13-1")
crt_w3 = Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name=k, value=str(v)) for k, v in
                            dict(w3, PRODUCTION_DAY="2025-10-14", DEFERRED_GAS_VOL_SM3=2380000.0, DEFERRED_COND_VOL_STM3=35.8).items()])

check("011 intents: 007/009/002/005 close, 006 create",
      [agent._write_intent(t)[0] for t in (T007, T009, T002, T005, T006)] == ["close", "close", "close", "close", "create"])
s7 = st(T007)
b_c = agent._write_block_reason(crt_w3, s7, DW(T)) or ""
b_u = agent._write_block_reason(end_upd("W_WELL-02_2025-10-13-1", "2025-10-15T23:59:59"), s7, DW(T)) or ""
check("011 007 shape: missing-day create -> CLOSE NO TIME", b_c.startswith("CLOSE NO TIME: WRITES BLOCKED"))
check("011 007 shape: END_DATE update -> CLOSE NO TIME", b_u.startswith("CLOSE NO TIME: WRITES BLOCKED"))
check("011 CLOSE NO TIME is a clarification guard", agent._clarify_guard(b_c) == ("CLOSE NO TIME", "the exact restart/end timestamp"))
s7.clarify_blocks.append(agent._clarify_guard(b_c))
r7 = agent._blocked_write_block(Req_WrfmRespond(message="done", outcome="ok_answer", ground_refs=[]), s7) or ""
check("011 007 shape: ok_answer -> BLOCKED WRITE -> clarification (0 writes)", r7.startswith("BLOCKED WRITE: CLOSE NO TIME"))
s9 = st(T009)
b9 = agent._write_block_reason(end_upd("W_WELL-02_2025-10-13-1", "2025-10-15T00:00:00"), s9, DW(T)) or ""
check("011 009 shape: END_DATE update blocked -> clarification guard (outcome unchanged)", agent._clarify_guard(b9) is not None)
b2 = agent._write_block_reason(end_upd("W_WELL-02_2025-10-13-1", "2025-10-15T11:30:00"), st(T002), DW(T)) or ""
check("011 002 shape (stated time): END_DATE update not CLOSE-blocked", not b2.startswith("CLOSE NO TIME") and not b2.startswith("END TIME"))
b2c = agent._write_block_reason(crt_w3, st(T002), DW(T)) or ""
check("011 002 shape: create not CLOSE-blocked", not b2c.startswith("CLOSE NO TIME"))
s6 = st(T006); s6.required = {}
b6 = agent._write_block_reason(Req_WrfmDataCreate(table_name="deferments", field_values=[WrfmKeyValue(field_name="EVENT_KEY", value="W_WELL-05_2025-10-15-1"), WrfmKeyValue(field_name="WELL_NAME", value="WELL-05")]), s6, DW(T)) or ""
check("011 006 shape (create intent, 'closed on'): not CLOSE-blocked", not b6.startswith("CLOSE NO TIME"))
b5 = agent._write_block_reason(end_upd("W_WELL-02_2025-10-13-1", "2025-10-15T00:00:00"), st(T005, "Operations Supervisor"), DW(T)) or ""
print("  005 block:", b5[:90])
check("011 005 shape: authority guard still decides first if it blocks", not b5 or b5.startswith(("AUTHORITY", "CLOSE NO TIME")))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
