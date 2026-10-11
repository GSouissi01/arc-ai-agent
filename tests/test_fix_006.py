"""Offline test of fix-006 Step 1 (well refs): no network."""
import sys
sys.path.insert(0, sys.argv[1])
from ogchallenge_client.dtos import Req_WrfmRespond, WrfmGroundRef
import agent

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond

def refs(state, cited=()):
    r, _ = agent._enrich_refs(Req_WrfmRespond(message="m", outcome="ok_answer", ground_refs=list(cited)), state)
    return [(x.type, x.id) for x in r.ground_refs]

s = agent._TaskState(task_text="Add yesterday's missing deferment record for the ongoing WELL-03 shut-in.")
s.ref_wells.add("WELL-03"); s.event_targets.add("W_WELL-03_2025-10-13-1")
out = refs(s)
check("write-target well auto-cited", ("well", "WELL-03") in out and ("deferment_event", "W_WELL-03_2025-10-13-1") in out)
check("no duplicate when agent already cited it", refs(s, [WrfmGroundRef(type="well", id="WELL-03")]).count(("well", "WELL-03")) == 1)
s = agent._TaskState(task_text="Close the WELL-02 deferment")
s.event_targets.add("W_WELL-02_2025-10-13-1")
check("well of W_ event the task is about (from key)", ("well", "WELL-02") in refs(s))
s = agent._TaskState(task_text="Review the event")
s.event_targets.add("G_NOVA7_2025-12-29-1"); s.ref_wells.add("WELL-03")
out = refs(s)
check("G_ event: only acted-on wells", [x for x in out if x[0] == "well"] == [("well", "WELL-03")])
s = agent._TaskState(task_text="Summarise")
s.seen_ids.update({"WELL-01", "WELL-02"})
check("listed-only wells not added", not [x for x in refs(s) if x[0] == "well"])
check("prompt: cite every well", "Cite every well you wrote or asked about." in agent.SYSTEM_PROMPT)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(fails)
