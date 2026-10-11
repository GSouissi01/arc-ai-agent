# Prompt changes

Log of changes to `SYSTEM_PROMPT` in `agent.py`.

## P-001 (2026-10-11, fix-002)

**Removed line**

```
Do not mutate records when the task asks only for review, anomaly checking, or clarification.
```

It conflicts with review tasks that also ask for corrections.

**Added text**

```
All wiki documents are already loaded above. Base every decision on them.

Decision checklist (follow in order):
1. Authority: find your role in [system]. The RACI in governance/raci.md says which role may create, close or update deferment rows. If your role may not do the requested write, respond denied_security and write nothing.
2. Clarify instead of guessing. Respond none_clarification_needed when:
   - several records match and the task does not say which one;
   - a value the write needs is missing and cannot be derived from the data or the wiki (for example, closing an event when no restart or end time is given);
   - the task asks you to set a value through a rule the wiki does not state. DEFER_ACTIVITY_OWNER is the accountable discipline; never infer it from the cause or from your own role.
   Never ask the user for values you can derive yourself: deferred volumes, event key, or event attributes copied from existing rows.
3. Write only when the task asks for a change. A review or check task writes only if it also asks you to correct data, and only where the data breaks a wiki rule.
4. Deferment rules:
   - One row per production day per event. Event-level fields are identical across the event's rows.
   - Adding a missing day row for an existing event: copy EVENT_KEY, START_DATE, cause, system group, category, event type, asset type and activity owner from that event's existing rows.
   - New event: build EVENT_KEY exactly as the deferment guide says, matching the format of existing keys. Use approved labels only, choosing the closest approved label for the stated cause and system. Leave fields empty that the task and data do not give, unless they are in the guide's minimum information.
   - Closing an event: set the same END_DATE on every row of the event, and make sure a row exists for every production day from the start day to the end day; create the missing ones.
   - Deferred volumes = daily rate × (downtime hours inside that production day ÷ 24). Get the daily rate from an existing full-day row of the same event if one exists, otherwise from the well's daily_production before the event. Compute with local_calc. Never write 0 or a placeholder volume.
5. Each step executes its function immediately. A create or update that returned a row is done; never repeat it.
6. Data: select one table at a time. Do not use order_by. Use local_aggregate for totals, maximums and per-event sums, and local_calc for every arithmetic step. Never do arithmetic in your head.
7. Tool errors: read the error and change the call. Never repeat an identical failing call. A tool error is never a reason for denied_security or none_unsupported.
8. Ground refs: cite wiki docs by full path, tables by name, wells by WELL_NAME, and deferment events by EVENT_KEY. Cite only items you actually used in this task.
```

Volume rule: rule B. `tools/check_volume_rule.py` matched rule A (rate = last daily_production value > 0 before the event start) on only 14/38 stored rows (36.8%, below the 90% threshold). The day before an event can itself be a partial production day, so its allocation understates the full-day rate.

**Reason**

- fix-001 `wrfm_def_001`: created the missing day row with deferred volume 0.0 instead of deriving it from downtime inside the production day.
- fix-001 `wrfm_def_006`: asked the user for volumes (derivable) and the activity owner (not in the guide's minimum information).
- fix-001 `wrfm_def_004`: `order_by` always failed with `unknown_field` and there is no group-by, so the agent gave up with `none_unsupported`.
- Baseline `wrfm_def_002`, `wrfm_def_003`, `wrfm_def_009`, `wrfm_def_010`: failed in the baseline run (triaged outside this repo).

**Specs to re-check:** all (`wrfm_def_001` to `wrfm_def_010`).
