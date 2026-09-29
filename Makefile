# Run the triage system and grade it with agentic_exercise_grader. `make help` lists targets.
#
# Model targets use the Claude subscription: an incident is about 4 agent calls and 2 minutes.
# Runs go to separate folders so the "newest run" of an incident is never a fault-injected one:
#   out/runs/<id>-<time>/                         make incidents, make incident-<id>
#   out/variants/<variant>/runs/<id>-<time>/      make variants, make variant-<variant>
#   out/stability/<id>/runs/<id>-<time>/          make stability ID=<id> N=<n>
# Each graded run gets a grade.txt next to its result.json.

SHELL := bash
.SHELLFLAGS := -o pipefail -c
.DEFAULT_GOAL := help

GRADER ?= ../agentic_exercise_grader
GRADE := python3 $(GRADER)/grade.py
TRIAGE := uv run triage
INCIDENTS := INC-2043 INC-2051 INC-2062
ID ?= INC-2043
N ?= 5

# Variant id -> incident and the fault flags that set it up (golden/variants.json, D10).
VARIANT_V-2043-no-changes      := INC-2043 --empty-tool search_changes
VARIANT_V-2043-no-logs         := INC-2043 --empty-tool search_logs
VARIANT_V-2043-historian-fails := INC-2043 --fail-agent change_historian
VARIANT_V-2043-analyst-timeout := INC-2043 --delay-agent log_analyst=200
VARIANT_V-2051-no-changes      := INC-2051 --empty-tool search_changes
VARIANT_V-2062-no-runbook-hits := INC-2062
VARIANT_V-2043-tool-cap-3      := INC-2043 --max-tool-calls 3
VARIANTS := V-2043-no-changes V-2043-no-logs V-2043-historian-fails V-2043-analyst-timeout \
            V-2051-no-changes V-2062-no-runbook-hits
EXPLORATORY := V-2043-tool-cap-3  # no fixed expectation: compare with the uncapped runs

# newest <id>-<time>/ folder under a directory; full runs only, not debug-agent folders
newest = $$(ls -dt $(1)/$(2)-20*/ 2>/dev/null | head -1)

.PHONY: help test test-slow tools runbook incidents summary variants stability check-grader

help:
	@echo "No model:"
	@echo "  make test                 fast tests"
	@echo "  make tools                run the tool cases and grade them"
	@echo "  make grade-<id>           grade the newest run of <id> again"
	@echo "  make summary              one line per incident from its newest grade.txt"
	@echo "Model (Claude subscription):"
	@echo "  make incidents            run and grade $(INCIDENTS), one at a time; fails at the end if any failed"
	@echo "  make incident-<id>        run and grade one incident"
	@echo "  make runbook              run the runbook lookup cases and grade them"
	@echo "  make variants             run and grade every variant: $(VARIANTS)"
	@echo "  make variant-<variant>    one variant, including the exploratory $(EXPLORATORY)"
	@echo "  make stability ID=<id> N=<n>   N runs of one incident, graded together (pass rate per check)"
	@echo "  make test-slow            tests against the real model"
	@echo "In parallel: make -j3 -k incident-INC-2043 incident-INC-2051 incident-INC-2062; make summary"

check-grader:
	@test -f $(GRADER)/grade.py || { echo "grader not found at $(GRADER); set GRADER=..."; exit 2; }

test:
	uv run pytest

test-slow:
	uv run pytest -m slow

tools: check-grader
	$(TRIAGE) eval-tools $(GRADER)/golden/tools.json -o out/tools.json
	$(GRADE) tools out/tools.json | tee out/tools.grade.txt

runbook: check-grader
	$(TRIAGE) eval-runbook $(GRADER)/golden/variants.json -o out/runbook.json
	$(GRADE) runbook out/runbook.json | tee out/runbook.grade.txt

incident-%: check-grader
	$(TRIAGE) run $*
	@$(MAKE) --no-print-directory grade-$*

grade-%: check-grader
	@d=$(call newest,out/runs,$*); test -n "$$d" || { echo "no run for $* in out/runs"; exit 2; }; \
	 $(GRADE) run $* $${d}result.json --trace $${d}trace.jsonl | tee $${d}grade.txt

incidents:
	@failed=; for id in $(INCIDENTS); do $(MAKE) --no-print-directory incident-$$id || failed="$$failed $$id"; done; \
	 $(MAKE) --no-print-directory summary; \
	 test -z "$$failed" || { echo "hard checks failed or run crashed:$$failed"; exit 1; }

summary:
	@for id in $(INCIDENTS); do \
	   d=$(call newest,out/runs,$$id); \
	   if [ -z "$$d" ] || [ ! -f "$${d}grade.txt" ]; then printf "%-9s no graded run\n" $$id; continue; fi; \
	   fails=$$(grep -c '^  FAIL' $${d}grade.txt || true); warns=$$(grep -c '^  warn' $${d}grade.txt || true); \
	   printf "%-9s %-4s hard failures=%s warnings=%s  %s\n" $$id "$$( [ $$fails = 0 ] && echo PASS || echo FAIL)" $$fails $$warns $$d; \
	 done

variant-%: check-grader
	@args="$(VARIANT_$*)"; test -n "$$args" || { echo "unknown variant $*"; exit 2; }; \
	 id=$${args%% *}; dir=out/variants/$*; \
	 echo "== $*: triage run $$args"; \
	 TRIAGE_OUT_DIR=$$dir $(TRIAGE) run $$args; \
	 d=$$(ls -dt $$dir/runs/$$id-20*/ 2>/dev/null | head -1); test -n "$$d" || { echo "no run for $*"; exit 2; }; \
	 $(GRADE) variant $* $${d}result.json --trace $${d}trace.jsonl | tee $${d}grade.txt

variants:
	@failed=; for v in $(VARIANTS); do $(MAKE) --no-print-directory variant-$$v || failed="$$failed $$v"; done; \
	 test -z "$$failed" || { echo "variants with hard failures or crashes:$$failed"; exit 1; }

stability: check-grader
	@dir=out/stability/$(ID); for i in $$(seq $(N)); do echo "== $(ID) run $$i of $(N)"; TRIAGE_OUT_DIR=$$dir $(TRIAGE) run $(ID) || true; done; \
	 $(GRADE) run $(ID) $$dir/runs/$(ID)-20*/result.json | tee $$dir/grade.txt
