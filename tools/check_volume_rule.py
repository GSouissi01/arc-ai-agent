"""
Check the deferred-volume rule against stored deferment rows.

Read-only: starts a dev session (same as tools/dump_wiki.py) to get a
task-scoped WRFM client, then only calls data_select. Never calls the LLM,
never runs the agent, never calls complete_task or submit_session.

Rule checked, per deferment row:
    downtime_h = hours of the event (START_DATE -> END_DATE, empty = ongoing)
                 inside that row's PRODUCTION_DAY (00:00-24:00)
    rate       = well's ALLOC_GAS_SM3 / ALLOC_COND_M3 on the last REPORT_DAY
                 strictly before the event start date with value > 0
    expected   = rate * downtime_h / 24

Usage (from sample-agent-wrfm/):
    .venv\\Scripts\\python.exe tools\\check_volume_rule.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import BENCHMARK_ID, _build_platform_client  # noqa: E402  (loads .env)
from ogchallenge_client import WrfmClient  # noqa: E402

OUT_DIR = ROOT / "runs" / "data"
TOLERANCE = 0.01
VOLUMES = [  # (deferments field, daily_production field)
    ("DEFERRED_GAS_VOL_SM3", "ALLOC_GAS_SM3"),
    ("DEFERRED_COND_VOL_STM3", "ALLOC_COND_M3"),
]


def _select_all(wrfm: WrfmClient, table: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = wrfm.data_select(table, limit=1000, offset=offset)
        rows.extend(page.rows)
        if page.next_offset == -1 or page.next_offset <= offset:
            return rows
        offset = page.next_offset


def _num(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _dt(value) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    return datetime.fromisoformat(str(value).strip())


def _day(value) -> date:
    return datetime.fromisoformat(str(value).strip()[:10]).date()


def _downtime_hours(row: dict) -> float:
    day_start = datetime.combine(_day(row["PRODUCTION_DAY"]), datetime.min.time())
    day_end = day_start + timedelta(days=1)
    start = _dt(row.get("START_DATE"))
    end = _dt(row.get("END_DATE")) or day_end  # empty END_DATE = ongoing
    if start is None:
        return float("nan")
    overlap = min(end, day_end) - max(start, day_start)
    return max(overlap.total_seconds(), 0.0) / 3600.0


def _rate(production: list[dict], well: str, before: date, field: str) -> tuple[float | None, str | None]:
    best_day, best_val = None, None
    for p in production:
        if p.get("WELL_NAME") != well:
            continue
        val = _num(p.get(field))
        if val is None or val <= 0:
            continue
        d = _day(p["REPORT_DAY"])
        if d < before and (best_day is None or d > best_day):
            best_day, best_val = d, val
    return best_val, (best_day.isoformat() if best_day else None)


def _matches(expected: float | None, stored: float | None) -> bool:
    if expected is None or stored is None or expected != expected:  # None or NaN
        return False
    if stored == 0:
        return abs(expected) < 1e-9
    return abs(expected - stored) <= TOLERANCE * abs(stored)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the deferred-volume rule (read-only)")
    parser.add_argument("--spec", default="wrfm_def_001", help="Spec whose task_id scopes the data client")
    parser.add_argument("--workspace", default="dev-wrfm")
    parser.add_argument("--start-task", action="store_true",
                        help="Call start_task first (only if data calls fail without it)")
    args = parser.parse_args()

    api = _build_platform_client()
    session = api.start_session(
        benchmark=BENCHMARK_ID,
        workspace=args.workspace,
        name="wrfm volume rule check (no agent)",
        architecture="read-only data check",
    )
    print(f"Session ID: {session.session_id}")
    status = api.session_status(session.session_id)
    matches = [t for t in status.tasks if t.spec_id == args.spec]
    if not matches:
        known = ", ".join(t.spec_id for t in status.tasks) or "none"
        raise SystemExit(f"Unknown spec_id {args.spec!r}. Known: {known}")
    task = matches[0]
    if args.start_task:
        api.start_task(task)
        print(f"Started task {task.task_id} (not completed; no agent run)")
    wrfm = api.get_wrfm_client(task)

    deferments = _select_all(wrfm, "deferments")
    production = _select_all(wrfm, "daily_production")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, rows in (("deferments", deferments), ("daily_production", production)):
        (OUT_DIR / f"{name}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"Saved {len(rows)} rows -> {(OUT_DIR / f'{name}.json').relative_to(ROOT)}")

    counts = {field: 0 for field, _ in VOLUMES}
    both = 0
    mismatches = []
    for row in deferments:
        downtime = _downtime_hours(row)
        start = _dt(row.get("START_DATE"))
        detail = {"EVENT_KEY": row.get("EVENT_KEY"), "WELL_NAME": row.get("WELL_NAME"),
                  "PRODUCTION_DAY": row.get("PRODUCTION_DAY"), "START_DATE": row.get("START_DATE"),
                  "END_DATE": row.get("END_DATE"), "downtime_h": round(downtime, 4)}
        row_ok = True
        for dfield, pfield in VOLUMES:
            rate, rate_day = (None, None)
            if start is not None:
                rate, rate_day = _rate(production, row.get("WELL_NAME"), start.date(), pfield)
            expected = rate * downtime / 24 if rate is not None else None
            stored = _num(row.get(dfield))
            ok = _matches(expected, stored)
            counts[dfield] += ok
            row_ok &= ok
            detail[dfield] = {"stored": stored, "expected": None if expected is None else round(expected, 2),
                              "rate": rate, "rate_day": rate_day}
        both += row_ok
        if not row_ok:
            mismatches.append(detail)

    total = len(deferments) or 1
    print(f"\nDeferment rows: {len(deferments)}")
    for field, _ in VOLUMES:
        print(f"  {field}: {counts[field]}/{len(deferments)} match within 1% ({counts[field] / total:.1%})")
    print(f"  Both volumes: {both}/{len(deferments)} ({both / total:.1%})")
    print(f"\nExample mismatches ({min(5, len(mismatches))} of {len(mismatches)}):")
    for m in mismatches[:5]:
        print("  " + json.dumps(m))
    print("\nDone. No task completed, no session submitted, no LLM called.")


if __name__ == "__main__":
    main()
