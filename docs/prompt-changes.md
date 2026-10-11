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

## P-002 (2026-10-11, fix-003)

**Replaced: closing bullet (rule 4)**

Old:

```
   - Closing an event: set the same END_DATE on every row of the event, and make sure a row exists for every production day from the start day to the end day; create the missing ones.
```

New:

```
   - Closing an event: run local_event_check first. Create every missing production-day row WITH END_DATE already set to the closing timestamp; the closing-day row is prorated from 00:00 to END_DATE. Then update END_DATE on all existing rows of the event. Then run local_event_check again and confirm missing is empty and every row has the same END_DATE.
```

**Replaced: volume bullet (rule 4, was rule B from P-001)**

Old:

```
   - Deferred volumes = daily rate × (downtime hours inside that production day ÷ 24). Get the daily rate from an existing full-day row of the same event if one exists, otherwise from the well's daily_production before the event. Compute with local_calc. Never write 0 or a placeholder volume.
```

New:

```
   - Deferred volumes: deferred volume for a row = R × downtime_h / 24, where R is the well's full-day rate. Get downtime_h and uptime_h from local_downtime, and do the arithmetic with local_calc. Choose R in this order:
     a. Existing event (its rows already exist): always use rate_per_well for that well from local_event_check.
     b. New event with no rows yet that is a full stop (EVENT_TYPE DOWN, or type not stated): daily_production ALLOC values are ACTUAL production, already reduced by downtime, so R = ALLOC on a production day with uptime (e.g. the event start day) × 24 / uptime_h for that day.
     c. New event that is a partial curtailment (EVENT_TYPE CONSTRAINT: a rate cut, restriction or limit): the lost rate cannot be derived from the data; respond none_clarification_needed for the volumes instead of guessing.
     Never use a day's ALLOC directly as the rate, and never write 0 or a placeholder volume.
```

**Added to rule 3**

```
   For review/correct tasks on an event, run local_event_check: create rows for every missing (well, day) pair and fix rows that break a guide rule. Do not change fields the guide does not require.
```

**Added rule 9**

```
9. Answer format: if the task asks for "only" a value (a figure, a number, a name), the message must be exactly that value and nothing else: no words, no units, no thousands separators, at most 2 decimals, as computed. Otherwise keep messages concise.
```

**Evidence for the volume rule** (offline, `runs/data/*.json`, wrfm_def_001 snapshot, 37 rows with downtime > 0):

- R_row = deferred × 24 / downtime_h is constant within 1% per event and well: 30/30 (100%; only 2 groups have more than one row with downtime).
- R_alloc = ALLOC(day) × 24 / uptime_h matches R_row within 1% on 20/30 rows (66.7%). All 20 matches are EVENT_TYPE DOWN; all 10 mismatches are EVENT_TYPE CONSTRAINT (comments: rate cut, restriction, choke limit), where the deferred volume is a partial loss.

**Reason**

- fix-002 `wrfm_def_006`: used the event day's ALLOC as the rate (553,125 instead of 737,500).
- fix-002 `wrfm_def_001`: used the start day's reduced ALLOC as the rate (2,222,986.11 instead of 2,380,000).
- fix-002 `wrfm_def_002`: used the start day's ALLOC as the rate; the closing-day row was created without END_DATE.
- fix-002 `wrfm_def_004`: the right figure, but the message was a sentence instead of only the number.
- fix-002 `wrfm_def_010`: the review missed a required create (missing production-day row).
- fix-002 `wrfm_def_003`, `wrfm_def_010`: DEFER_ACTIVITY_OWNER inferred from the cause (now also blocked in code).
- fix-002 `wrfm_def_005`: Operations Supervisor closed an event (now blocked in code by the RACI authority guard).

**Specs to re-check:** all (`wrfm_def_001` to `wrfm_def_010`).

## P-003 (2026-10-11, fix-004)

**Rule 4, closing bullet: prepended**

```
Only when the task states the restart/end time explicitly.
```

Now reads: `- Closing an event: Only when the task states the restart/end time explicitly. Run local_event_check first. ...`

**Rule 4, new-event bullet: replaced**

Old:

```
Leave fields empty that the task and data do not give, unless they are in the guide's minimum information.
```

New:

```
Fill every required field. For a new event choose the closest approved labels (cause, system group, activity owner) from the guide. For a row of an existing event copy them.
```

**Rule 3: added**

```
   For a group event (G_ key), also create rows for missing_wells reported by local_event_check.
```

**Evidence**

- `data_schema` marks every deferments field required except COMMENTS and END_DATE, including DEFER_ACTIVITY_OWNER (with enum lists).

**Reason**

- fix-003 `wrfm_def_009`: closed the event with an invented END_DATE (today 00:00) when the task gave no restart time.
- fix-003 `wrfm_def_006`: create retried without DEFER_ACTIVITY_OWNER and was rejected (`missing_required_field`); the agent then asked the user.
- fix-003 `wrfm_def_002`: create rejected 4 times with `missing_required_field`.
- fix-003 `wrfm_def_010`: group event review missed a create for an affected well without rows.

No change for `wrfm_def_004` (pending investigation).

**Specs to re-check:** all (`wrfm_def_001` to `wrfm_def_010`).
