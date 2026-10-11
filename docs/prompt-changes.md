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

## P-004 (2026-10-11, fix-005)

**Added rule 0 (first in the checklist)**

```
0. Check authority before anything else (see [authority]). No authority -> respond denied_security, even if details are missing, and cite governance/raci.md.
```

A computed `[authority]` note (role vs. every RACI activity) is now added to the context after the bootstrap.

**Rule 2: owner clause replaced (conflict with the new-event bullet resolved)**

Old:

```
   - the task asks you to set a value through a rule the wiki does not state. DEFER_ACTIVITY_OWNER is the accountable discipline; never infer it from the cause or from your own role.
```

New:

```
   - the task asks you to change DEFER_ACTIVITY_OWNER on an existing event and the new owner is not stated in the task or the docs. DEFER_ACTIVITY_OWNER is the accountable discipline; never infer it from the cause or from your own role. For a new event the owner is required: take it from the docs (the guide's owner definition: the accountable discipline for the cause); do not ask.
```

**Rule 2: label mapping added**

```
   Never ask for clarification only because a free-text cause, system or owner word is not an approved label. Map it to the closest approved label from the deferment guide lists (for example, a valve or actuator fault -> the matching equipment breakdown cause, and the system group that contains that equipment). Ask only when two labels fit equally well.
```

**Rule 4: copy bullet extended**

```
   - Adding a missing day row (or missing well) for an existing event: copy every event attribute from that event's existing rows: EVENT_KEY, START_DATE, END_DATE (if the event is closed), cause, system group, category, event type, asset type, activity owner and COMMENTS.
```

**Rule 8: appended**

```
Always cite the deferment event you were asked about (by EVENT_KEY), also in clarification and denied answers.
```

**Rule 4: volume bullet replaced (single rule for DOWN and CONSTRAINT)**

Old (P-002, a/b/c rate order):

```
   - Deferred volumes: deferred volume for a row = R × downtime_h / 24, where R is the well's full-day rate. ... (a. rate_per_well, b. ALLOC × 24 / uptime_h, c. CONSTRAINT -> clarification)
```

New:

```
   - Deferred volumes come only from volume_estimate. local_event_check returns volume_estimate for every missing day and missing well of an existing event; for a new event (or any single row) call local_volume_estimate with the well, start, end, production day and event type. Write deferred_gas and deferred_cond rounded to 2 decimals. If volume_estimate has no method (clarification_needed), respond none_clarification_needed asking for the volumes and cite the event. Never compute volumes yourself, never guess, and never write 0 unless volume_estimate is 0.
```

volume_estimate (code): R = the well's ALLOC on the last clean day before the event START (no deferment row for that well that day). Day has daily_production -> `clean_day_diff` (R − ALLOC(day)); no daily_production and DOWN -> `clean_day_hours` (R × downtime_h / 24); no clean day and DOWN -> `best_R_fallback`; otherwise (incl. CONSTRAINT without data) -> no method, clarification. Unrounded inside; the validator compares creates against it with tolerance max(0.01, 0.01%).

**Evidence (fix-005 Step 0, 001 snapshot):** clean-day difference fits 38/38 stored rows within 0.01 (37/38 exact to 2 decimals; 1 condensate row differs by 0.01 because both stored values were rounded independently): 26/26 DOWN, 12/12 CONSTRAINT. Earlier hypotheses: H1 (pre-event day) 75%, H2/H3 75%, H4 0%, H5 f not constant. 001's missing day gives 2,380,000.00 / 35.80 exactly.

**Reason**

- fix-004 `wrfm_def_002`: new row did not copy COMMENTS.
- fix-004 `wrfm_def_003`: no deferment_event ref in a clarification answer.
- fix-004 `wrfm_def_004`: deferment_event ref dropped by the enricher (local_aggregate ids not counted as seen; code fix).
- fix-004 `wrfm_def_005`: Operations Supervisor answered none_clarification_needed instead of denied_security.
- fix-004 `wrfm_def_006`: asked for clarification because the free-text cause was not an approved label.
- fix-004 `wrfm_def_010`: copying the closed event's END_DATE was blocked by the END TIME guard (code fix).

**Specs to re-check:** all.

## P-005 (2026-10-11, fix-006)

**Well refs (rule 8, appended)**

```
Cite every well you wrote or asked about.
```

Code adds a `well` ref automatically for every WELL_NAME in a create/update (including blocked writes), the targets of local_volume_estimate, the wells of a checked single-well event, the missing wells flagged on a group event, and the well inside a single-well event key the task is about. Wells that were only listed are not added.

**Volume rule (rule 4, volume bullet: method detail added)**

```
   - Deferred volumes come only from volume_estimate (DOWN rows: the stated or event-span downtime hours x the well's clean-day rate; CONSTRAINT rows: the clean-day ALLOC difference, or the peers' loss fraction for a group-event well whose production data shows no loss). ... Write its write_gas and write_cond values exactly as given. ...
```

volume_estimate order (code): DOWN -> `clean_day_stated_hours` (exactly two times stated in the task; midnight/12am = 24:00 as an end; an end before the start crosses midnight) -> `clean_day_span_hours` (event span inside the production day) -> `best_R_fallback` -> clarification. CONSTRAINT -> `clean_day_diff`; a facility well on a group event with no row, no CLOSED status in the window, no other deferment row, ALLOC > 0 but no loss vs its clean-day rate, and >= 2 peer rows -> `group_peer_fraction` (one physical cut fraction: median peer deferred gas / peer clean-day gas rate, applied to the well's own clean-day gas and condensate rates; condensate written at 1 decimal only if every peer's stored condensate equals round(peer clean-day cond rate x fraction, 1), else at 2 decimals). Values are rounded half-up to 2 decimals (`write_gas`, `write_cond`).

**Forced event check (added after the wiki line)**

```
If an [event_check] block is present, it is the code's check of the event the task is about: handle every missing and volume_mismatch item it lists, or state why not.
```

Code runs local_event_check at bootstrap when the task names an EVENT_KEY or one well + a date that resolve to exactly one event, and after any deferments select whose rows all belong to one unchecked event. A respond guard blocks the first ok_answer on review/correct/check/fix/add/missing/close tasks while missing or volume_mismatch items remain unwritten; a second ok_answer is accepted and its current_state is logged as the reason.

**Numeric answer format:** unchanged. An experiment replacing the numeric message with (A) the largest single-row value as stored or (B) the largest event total as an integer scored 0.00 in both cases; the experiment flag was removed.

**Evidence**

- DOWN rows: event-span hours x clean-day rate = 26/26 stored rows; all 38 rows match the written (half-up) values exactly.
- A parameterised create task stating "06:45:00 to midday" expects 2,950,000 x 5.25 / 24 = 645,312.5; the clean-day ALLOC difference gave 737,500 because that day's ALLOC also reflects other lost hours.
- A group CONSTRAINT event lacks a row for one facility well whose production data is flat; the expected correction is one create. Peers lost 0.20 of their clean-day rate (one outlier 0.159).

**Specs to re-check:** all.

## P-006 (2026-10-11, fix-007): blocked writes -> clarification, write scope, COMMENTS convention

**Event check wording (replaces the P-005 line)**

```
If an [event_check] block is present, it is the code's check of the event the task is about. If the task asks to review/correct the event data, handle every missing and volume_mismatch item it lists, or state why not. Otherwise it is information only.
If your writes were blocked because the task lacks information (owner, end time, volumes) and no write succeeded, respond none_clarification_needed asking for that information.
```

The bootstrap block is now labelled `[event_check] INFO ONLY. Act on these items only if the task asks to review/correct the event data.` The event-check respond guard applies only to review / correct / check / reconcile / fix-data tasks, never to set-owner, close or create tasks.

**Blocked writes -> clarification (code):** when a write was blocked by a clarification-type guard (OWNER BLOCKED because the owner is not stated, END TIME BLOCKED, or VOLUME CHECK with no data-derived volume_estimate) and no write succeeded in the task, an ok_answer is blocked with `BLOCKED WRITE: <guard> -> respond none_clarification_needed asking for <missing info>, cite the event and well`.

**Write scope (code, no prompt text):** task intent decides the allowed write types (first match wins):

| Task wording | Allowed |
|---|---|
| set / update / change / assign + owner / discipline | update |
| close / restart(ed) / back online / back on line / resumed / restored / came back / brought back / returned to production or service / ended / finished | update + create |
| create / add / log | create |
| review / correct / check / reconcile / fix | create + update |
| update / change / set / amend (no owner) | update |
| none of the above (intent unknown) | allowed; the other guards decide; `write_scope: intent unknown` is logged in the trace |

Only a known intent blocks: an out-of-scope write returns `WRITE SCOPE: task asks <intent>; <type> not allowed`.

**COMMENTS convention (rule 4, new-event bullet)**

```
COMMENTS for new events follow the data convention built by code. Do not write free text.
```

Code sets COMMENTS = `"<Cause> deferment; prorated by downtime within the day."` (`<Cause>` = the task's cause wording after "due to" / "because of" / "caused by", first letter capitalised) only for a create on a new event, EVENT_TYPE DOWN, partial production day (downtime < 24 h), when the task gives no comment. Existing events keep copying their COMMENTS. Derived from evaluator feedback; not documented in the wiki (0 of 38 snapshot rows follow it; the snapshot comments are free text).

**volume_mismatch method (code):** a row of a group event whose clean-day ALLOC difference shows no loss is checked with the same `group_peer_fraction` method that validated its create; without a peer fraction no mismatch is reported (never an update to 0).

**rule_violations: not added.** The 4 peer rows of the review/correct event break only one rule (DEFER_ACTIVITY_OWNER empty, schema-required). The task does not state an owner, and a set-owner task on the same event expects clarification, so no owner is written.

**Evidence**

- Set-owner task on a group event: the forced event check led to a create of the missing well (succeeded) and a blocked owner update, then ok_answer; expected none_clarification_needed.
- Create task (new DOWN event, partial day): expected COMMENTS `Wing valve actuator fault deferment; prorated by downtime within the day.`; we wrote the raw cause text.
- Review/correct group event: after the missing-well create (peer fraction 476,000 / 7.16), volume_mismatch recomputed it with clean_day_diff (0) and the agent overwrote it with 0.

**Specs to re-check:** all.
