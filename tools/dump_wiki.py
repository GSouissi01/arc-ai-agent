"""
Dump WRFM wiki pages to runs/wiki/ for offline reading.

Read-only: starts a dev session (same as main.py --spec) to get a task-scoped
WRFM client, then only calls wiki_tree / wiki_load. Never calls the LLM,
never runs the agent, never calls complete_task or submit_session.

Usage (from sample-agent-wrfm/):
    .venv\\Scripts\\python.exe tools\\dump_wiki.py
    .venv\\Scripts\\python.exe tools\\dump_wiki.py --start-task
    .venv\\Scripts\\python.exe tools\\dump_wiki.py --extra some/other_page.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import BENCHMARK_ID, _build_platform_client  # noqa: E402  (loads .env)
from ogchallenge_client import ApiException, WrfmClient  # noqa: E402
from agent import _tree_paths  # noqa: E402  (shared wiki tree parser)

PAGES = [
    "deferment/deferment_guide.md",
    "governance/raci.md",
    "data_reference/schema.md",
]
PREFIX = "sy"  # truncated entry seen in the wiki tree
OUT_DIR = ROOT / "runs" / "wiki"


def _safe_out_path(wiki_path: str) -> Path:
    parts = [p for p in re.split(r"[\\/]+", wiki_path) if p not in {"", ".", ".."}]
    out = OUT_DIR.joinpath(*parts)
    if out.suffix != ".md":
        out = out.with_name(out.name + ".md")
    return out


def _save(wiki_path: str, content: str) -> None:
    out = _safe_out_path(wiki_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"  saved {wiki_path} -> {out.relative_to(ROOT)} ({len(content)} chars)")


def _load(wrfm: WrfmClient, wiki_path: str) -> bool:
    try:
        page = wrfm.wiki_load(wiki_path)
    except ApiException as exc:
        print(f"  FAILED {wiki_path}: {exc}")
        return False
    _save(wiki_path, page.content)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump WRFM wiki pages (read-only)")
    parser.add_argument("--spec", default="wrfm_def_001", help="Spec whose task_id scopes the wiki client")
    parser.add_argument("--workspace", default="dev-wrfm")
    parser.add_argument("--start-task", action="store_true",
                        help="Call start_task first (only if wiki calls fail without it)")
    parser.add_argument("--extra", action="append", default=[], help="Extra wiki path to load")
    args = parser.parse_args()

    api = _build_platform_client()
    session = api.start_session(
        benchmark=BENCHMARK_ID,
        workspace=args.workspace,
        name="wrfm wiki dump (no agent)",
        architecture="read-only wiki dump",
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

    try:
        tree = wrfm.wiki_tree().tree
    except ApiException as exc:
        hint = "" if args.start_task else " Retry with --start-task."
        raise SystemExit(f"wiki_tree failed: {exc}.{hint}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "_tree.txt").write_text(tree, encoding="utf-8")
    print(f"Wiki tree saved to {(OUT_DIR / '_tree.txt').relative_to(ROOT)}:\n{tree}\n")

    targets = list(PAGES) + list(args.extra)
    for path in _tree_paths(tree):
        if Path(path).name.lower().startswith(PREFIX) and path not in targets:
            targets.append(path)

    for path in targets:
        if path.endswith(".md"):
            _load(wrfm, path)
            continue
        # Not a page: treat as a folder, list it and load the .md files inside.
        try:
            sub = wrfm.wiki_tree(path=path).tree
        except ApiException as exc:
            print(f"  FAILED tree {path}: {exc}")
            continue
        print(f"  folder {path}:\n{sub}")
        for child in _tree_paths(sub):
            full = child if child.startswith(path + "/") else f"{path}/{child}"
            if full.endswith(".md"):
                _load(wrfm, full)

    print("\nDone. No task completed, no session submitted, no LLM called.")


if __name__ == "__main__":
    main()
