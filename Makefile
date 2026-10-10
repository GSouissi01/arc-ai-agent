.PHONY: sync run task

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -n "$(SPEC)" ]; then uv run python main.py --spec $(SPEC); else echo "usage: make task SPEC=wrfm_def_001"; exit 1; fi
