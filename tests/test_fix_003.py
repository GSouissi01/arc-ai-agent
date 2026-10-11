"""Offline test of fix-003: no network."""
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from ogchallenge_client.dtos import (
    Req_WikiLoad, Req_WrfmDataCreate, Req_WrfmDataSelect, Req_WrfmDataUpdate, Req_WrfmRespond,
    Resp_WrfmRespond, WrfmCondition, WrfmKeyValue,
)
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


def dt(start, end, day):
    return agent._local_downtime(agent.LocalDowntime(type="local_downtime", start=start, end=end, production_day=day))

# ---- local_downtime ----
check("downtime partial start day", dt("2025-09-14T13:15:00", None, "2025-09-14") == {"downtime_h": 10.75, "uptime_h": 13.25})
check("downtime full day ongoing", dt("2025-09-14T13:15:00", "", "2025-09-16") == {"downtime_h": 24.0, "uptime_h": 0.0})
check("downtime mid-day close", dt("2025-09-14T13:15:00", "2025-09-20T17:40:00", "2025-09-20")["downtime_h"] == round(17 + 40 / 60, 6))
check("downtime ongoing start 22:25", abs(dt("2026-01-01T22:25:00", None, "2026-01-01")["downtime_h"] - 1.583333) < 1e-5)
check("downtime outside day = 0", dt("2025-09-14T13:15:00", "2025-09-15T00:00:00", "2025-09-15")["downtime_h"] == 0.0)
check("downtime bad input -> error", dt("garbage", None, "2025-09-14").get("code") == "local_downtime_error")


# ---- local_event_check ----
def row(key, well, day, start, end, gas, cond, owner=""):
    return {"EVENT_KEY": key, "WELL_NAME": well, "PRODUCTION_DAY": day, "START_DATE": start, "END_DATE": end,
            "DEFERRED_GAS_VOL_SM3": gas, "DEFERRED_COND_VOL_STM3": cond, "DEFER_ACTIVITY_OWNER": owner}


S, E = "2026-02-05T04:00:00", "2026-02-08T12:00:00"
EVENT_ROWS = [
    row("EV1", "W-A", "2026-02-05", S, E, 2583333.33, 40.0, "Maintenance and Integrity"),   # 20 h of 3,100,000
    row("EV1", "W-A", "2026-02-06", S, "", 3100000.0, 48.0, "Maintenance and Integrity"),   # missing END_DATE
    row("EV1", "W-A", "2026-02-08", S, E, 1550000.0, 24.0, "Maintenance and Integrity"),    # 12 h
    row("EV1", "W-B", "2026-02-05", S, E, 1000000.0, 10.0, "Maintenance and Integrity"),    # 20 h of 1,200,000
    row("EV2", "W-C", "2026-02-01", "2026-02-01T06:00:00", "", 900000.0, 9.0),              # ongoing
    row("EV2", "W-C", "2026-02-02", "2026-02-01T06:00:00", "", 1200000.0, 12.0),
]


class DataWrfm:
    def __init__(self, rows=EVENT_ROWS):
        self.rows, self.calls = rows, []

    def data_select(self, table_name, fields=None, where_clause=None, order_by=None, limit=100, offset=0):
        self.calls.append(where_clause)
        key = where_clause[0].value
        hits = [r for r in self.rows if r["EVENT_KEY"] == key]
        page = hits[offset:offset + 2]  # force paging
        nxt = offset + 2 if offset + 2 < len(hits) else -1
        return SimpleNamespace(rows=page, next_offset=nxt)


st = agent._TaskState(today="2026-02-04")
res = agent._local_event_check(DataWrfm(), agent.LocalEventCheck(type="local_event_check", event_key="EV1"), st)
check("event start/end", res["start"] == S and res["end"] == E and res["wells"] == ["W-A", "W-B"])
miss = {(m["well"], m["day"]) for m in res["missing"]}
check("event missing pairs", miss == {("W-A", "2026-02-07"), ("W-B", "2026-02-06"), ("W-B", "2026-02-07"), ("W-B", "2026-02-08")})
check("event rows_without_end_date", res["rows_without_end_date"] == [{"well": "W-A", "day": "2026-02-06"}])
rate = res["rate_per_well"]
check("event rate per well (median)", abs(rate["W-A"]["gas"] - 3100000.0) < 1 and abs(rate["W-B"]["gas"] - 1200000.0) < 1)
check("event rows detail has downtime", next(r for r in res["rows"] if r["day"] == "2026-02-08")["downtime_h"] == 12.0)
check("event owners tracked", ("EV1", "maintenance and integrity") in st.owners)

st = agent._TaskState(today="2026-02-04T09:00:00")
res = agent._local_event_check(DataWrfm(), agent.LocalEventCheck(type="local_event_check", event_key="EV2"), st)
check("ongoing: missing through yesterday", [m["day"] for m in res["missing"]] == ["2026-02-03"] and res["end"] == "")
check("ongoing: no rows_without_end_date key", "rows_without_end_date" not in res)

rows_midnight = [row("EV3", "W-A", "2026-02-01", "2026-02-01T10:00:00", "2026-02-03T00:00:00", 1, 1)]
res = agent._local_event_check(DataWrfm(rows_midnight), agent.LocalEventCheck(type="local_event_check", event_key="EV3"), st)
check("END_DATE at 00:00 excludes that day", [m["day"] for m in res["missing"]] == ["2026-02-02"])
res = agent._local_event_check(DataWrfm(), agent.LocalEventCheck(type="local_event_check", event_key="NOPE"), st)
check("unknown event -> error", res.get("code") == "event_not_found")


# ---- authority + owner guards ----
RACI_MD = open(sys.argv[1] + "/runs/wiki/governance/raci.md", encoding="utf-8").read()
RACI = agent._parse_raci(RACI_MD)
check("raci parsed 4 activities", len(RACI) == 4 and RACI["close deferment rows"]["operations supervisor"] == "consulted")


def kv(**f):
    return [WrfmKeyValue(field_name=k, value=v) for k, v in f.items()]


def state(role, raci=RACI, task="Review and correct the deferment event data.", docs=()):
    s = agent._TaskState(role=role, raci=raci, task_text=task)
    s.loaded_docs.update(docs)
    return s


create = Req_WrfmDataCreate(table_name="deferments", field_values=kv(EVENT_KEY="EV1", WELL_NAME="W-A"))
close = Req_WrfmDataUpdate(table_name="deferments", field_values=kv(END_DATE=E),
                           where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value="EV1")])
check("PP create allowed", agent._write_block_reason(create, state("Production Programmer")) is None)
check("PP close allowed (time in task)", agent._write_block_reason(close, state("Production Programmer", task="Close the deferment, restart at 16:45")) is None)
msg = agent._write_block_reason(create, state("Operations Supervisor"))
check("OS create blocked", msg and msg.startswith("AUTHORITY BLOCKED") and "'consulted' for 'create deferment rows'" in msg)
msg = agent._write_block_reason(close, state("Operations Supervisor"))
check("OS close blocked", msg and "close deferment rows" in msg and "denied_security" in msg)
check("unknown role blocked", "not listed" in agent._write_block_reason(create, state("Intern")))
check("no table -> governance fallback blocks", agent._write_block_reason(create, state("Operations Supervisor", raci={}))
      .startswith("WRITE BLOCKED"))
check("no table + governance doc -> allowed", agent._write_block_reason(
    create, state("Operations Supervisor", raci={}, docs=["governance/raci.md"])) is None)
check("owner update maps to 'activity owner'", agent._write_activity(
    Req_WrfmDataUpdate(table_name="deferments", field_values=kv(DEFER_ACTIVITY_OWNER="X")), RACI) == "update deferment activity owner")

owner_create = Req_WrfmDataCreate(table_name="deferments",
                                  field_values=kv(EVENT_KEY="EV9", WELL_NAME="W-A", DEFER_ACTIVITY_OWNER="Process Engineering"))
msg = agent._write_block_reason(owner_create, state("Production Programmer", task="Create a deferment for the trip."))
check("fix-004: new event create with owner label -> allowed (no enum known)", msg is None)
check("owner in task text -> allowed", agent._write_block_reason(
    owner_create, state("Production Programmer", task="Create a deferment for the trip; the owner is process engineering.")) is None)
s = state("Production Programmer")
agent._track_owners([{"EVENT_KEY": "EV9", "DEFER_ACTIVITY_OWNER": "Process Engineering"}], s)
check("owner copied from same event -> allowed", agent._write_block_reason(owner_create, s) is None)
s = state("Production Programmer")
agent._track_owners([{"EVENT_KEY": "OTHER", "DEFER_ACTIVITY_OWNER": "Process Engineering"}], s)
check("fix-004: new event create, owner seen elsewhere -> allowed", agent._write_block_reason(owner_create, s) is None)
owner_update = Req_WrfmDataUpdate(table_name="deferments", field_values=kv(DEFER_ACTIVITY_OWNER="Well Services"),
                                  where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value="EV9")])
check("owner update inferred -> blocked", agent._write_block_reason(owner_update, state("Production Programmer"))
      .startswith("OWNER BLOCKED"))
check("empty owner not blocked", agent._write_block_reason(Req_WrfmDataCreate(
    table_name="deferments", field_values=kv(EVENT_KEY="EV9", DEFER_ACTIVITY_OWNER="")), state("Production Programmer")) is None)


# ---- loop-level: bootstrap parses role/today/RACI; OS close blocked; local tools; select tracks owners ----
TREE = ".\n|-- governance/\n|   `-- raci.md\n"


class FakeLLM:
    def __init__(self, script):
        self.script, self.inputs = list(script), []
        self.responses = SimpleNamespace(parse=self.parse)

    def parse(self, model, input, text_format, **kw):
        self.inputs.append(input)
        return SimpleNamespace(output_parsed=self.script.pop(0), usage=None)


class LoopWrfm(DataWrfm):
    def __init__(self, role):
        super().__init__()
        self.role, self.dispatched = role, []

    def system(self):
        return SimpleNamespace(model_dump_json=lambda: json.dumps({"current_user": "u", "role": self.role, "today": "2026-02-04"}))

    def wiki_tree(self, path=""): return SimpleNamespace(tree=TREE)
    def data_schema(self): return SimpleNamespace(model_dump_json=lambda: '{"tables": {}}')

    def dispatch(self, fn):
        self.dispatched.append(fn)
        if isinstance(fn, Req_WikiLoad):
            return SimpleNamespace(content=RACI_MD)
        if isinstance(fn, Req_WrfmDataSelect):
            return SimpleNamespace(rows=[{"EVENT_KEY": "EV5", "DEFER_ACTIVITY_OWNER": "Well Services"}],
                                   model_dump_json=lambda exclude_none=True: '{"rows": []}')
        return Resp_WrfmRespond()

    def respond(self, message, outcome, ground_refs=None): pass


def step(fn):
    return agent.NextStep(current_state="st", plan=["p"], function=fn)


def run(role, script):
    llm, wrfm = FakeLLM(script), LoopWrfm(role)
    api = SimpleNamespace(get_wrfm_client=lambda t: wrfm, log_llm=lambda **k: None)
    agent.make_llm_client = lambda cfg: llm
    agent.run_agent(api, SimpleNamespace(spec_id="s", task_id="t", task_text="Close the event; restart at 16:45."),
                    llm_config=agent.LLMConfig(provider="openai", model="m", api_key="k"))
    return llm, wrfm


RESPOND = step(Req_WrfmRespond(message="x", outcome="denied_security"))
llm, wrfm = run("Operations Supervisor", [step(close), RESPOND])
check("loop: OS close blocked, not dispatched", not any(isinstance(f, Req_WrfmDataUpdate) for f in wrfm.dispatched)
      and "AUTHORITY BLOCKED" in str(llm.inputs[1]))
llm, wrfm = run("Production Programmer", [step(close), RESPOND])
check("loop: PP close dispatched", any(isinstance(f, Req_WrfmDataUpdate) for f in wrfm.dispatched))

llm, wrfm = run("Production Programmer", [
    step(agent.LocalDowntime(type="local_downtime", start=S, end=None, production_day="2026-02-05")),
    step(agent.LocalEventCheck(type="local_event_check", event_key="EV1")),
    step(Req_WrfmDataSelect(table_name="deferments")),
    step(Req_WrfmDataUpdate(table_name="deferments", field_values=kv(DEFER_ACTIVITY_OWNER="Well Services"),
                            where_clause=[WrfmCondition(field_name="EVENT_KEY", operator="eq", value="EV5")])),
    RESPOND])
h = str(llm.inputs[-1])
check("loop: local_downtime result", "RESULT of local_downtime" in h and '"downtime_h": 20.0' in h)
check("loop: local_event_check result", "RESULT of local_event_check" in h and '"missing"' in h)
check("loop: local tools not dispatched", not any(isinstance(f, (agent.LocalDowntime, agent.LocalEventCheck)) for f in wrfm.dispatched))
check("loop: owner seen in select -> update allowed", any(isinstance(f, Req_WrfmDataUpdate) for f in wrfm.dispatched))

# ---- prompt ----
P = agent.SYSTEM_PROMPT
check("prompt: volumes only from volume_estimate (fix-005)", "Deferred volumes come only from volume_estimate" in P)
check("prompt: rule B removed", "existing full-day row of the same event" not in P)
check("prompt: closing bullet", "Create every missing production-day row WITH END_DATE" in P)
check("prompt: rule 3 addition + rule 9", "For review/correct tasks on an event, run local_event_check" in P and "9. Answer format" in P)
from openai.lib._pydantic import to_strict_json_schema
check("strict schema builds", "local_event_check" in json.dumps(to_strict_json_schema(agent.NextStep)))

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
