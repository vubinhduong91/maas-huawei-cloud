SHELL := /bin/bash
COMPOSE := docker compose

# Lab mode: A = client -> APISIX -> MaaS (production-like, tap off)
#           B = client -> APISIX -> tap -> MaaS
#           C = client -> tap(:9091) -> MaaS (APISIX still up on :9080 in mode B config)
MODE ?= A
export LAB_MODE := $(if $(filter C,$(MODE)),B,$(MODE))
export LAB_UID := $(shell id -u)
export LAB_GID := $(shell id -g)
ifneq ($(MODE),A)
export COMPOSE_PROFILES := tap
endif

.PHONY: up down probe smoke render-check logs clean-logs tap-test tap-smoke diff test-phase2 dashboard dashboard-down analyze test-analyze scenario report keyorder-up keyorder-down keyorder test-keyorder

# Call MaaS directly to verify auth header / count_tokens / model names.
probe:
	bash scripts/probe-maas.sh

up:
	mkdir -p logs/apisix logs/tap logs/raw/pre logs/raw/post reports
	chmod 777 logs/apisix  # written by the non-root apisix user inside the container
	@if [[ "$(MODE)" == A ]]; then $(COMPOSE) --profile tap rm -sf tap >/dev/null 2>&1 || true; fi
	$(COMPOSE) up -d --build --force-recreate
	@echo "lab mode $(MODE): APISIX http://localhost:9080$(if $(filter A,$(MODE)),, | tap mode C http://localhost:9091)"

# -v also drops the volume holding the rendered config (it contains MAAS_API_KEY).
down:
	$(COMPOSE) --profile tap --profile dashboard --profile keyorder down -v

smoke:
	bash scripts/smoke.sh

# Print the rendered apisix.yaml with all keys masked.
render-check:
	$(COMPOSE) run --rm --no-deps apisix-render python /src/render.py --redacted --out -

logs:
	$(COMPOSE) logs -f apisix $(if $(filter A,$(MODE)),,tap)

clean-logs:
	rm -rf logs/apisix/* logs/tap/*

# Offline tap tests against a fake upstream (no MaaS call).
tap-test:
	$(COMPOSE) --profile tap build tap
	$(COMPOSE) --profile tap run --rm --no-deps -T tap python test_tap.py

# Phase 2 checks against real MaaS; expects `make up MODE=B`.
tap-smoke:
	bash scripts/tap-smoke.sh

# Diff the body APISIX logged with the body the tap received (mode B).
diff:
	python3 scripts/diff_bodies.py

# Full Phase 2 run: fresh logs, mode B, Phase 1 smoke through the tap, then tap-smoke.
test-phase2:
	$(MAKE) tap-test
	$(MAKE) down
	$(MAKE) clean-logs
	$(MAKE) up MODE=B
	bash scripts/smoke.sh
	bash scripts/tap-smoke.sh

# View-only APISIX dashboard (http://localhost:9180/ui/) mirroring the running lab config,
# keys masked. Re-run after changing the lab config. Mode is detected from the tap container.
dashboard:
	@grep -q '^DASHBOARD_ADMIN_KEY=.' .env || echo "DASHBOARD_ADMIN_KEY=$$(openssl rand -hex 16)" >> .env
	$(COMPOSE) --profile dashboard up -d etcd apisix-dashboard
	@mode=$$($(COMPOSE) --profile tap ps -q tap 2>/dev/null | grep -q . && echo B || echo A); \
	  echo "mirroring LAB_MODE=$$mode"; \
	  LAB_MODE=$$mode $(COMPOSE) --profile dashboard run --rm -T dashboard-loader
	@echo "dashboard: http://localhost:9180/ui/  admin key: $$(grep '^DASHBOARD_ADMIN_KEY=' .env | cut -d= -f2-)"

dashboard-down:
	$(COMPOSE) --profile dashboard rm -sf etcd apisix-dashboard dashboard-loader

# Phase 4: tap records -> reports/report.md + reports/requests.csv.
# Extra options: make analyze ARGS="--since 2026-10-11T08:00 --window-min 60"
analyze:
	python3 analyze.py $(ARGS)

# Offline analyzer tests on synthetic records.
test-analyze:
	python3 scripts/test_analyze.py

# Phase 5 scenarios (lab up in mode B): make scenario S=S2   |   make scenario S="S1 S4"   |   make scenario S=all
scenario:
	python3 scenarios/run.py $(S)

# Phase 6: final report = labelled scenario sessions + docs/findings.md appended at the end.
# make report ARGS="--since 2026-10-11T10:12"
report:
	python3 analyze.py --labelled-only --findings docs/findings.md $(ARGS)

# ---- Key-order drift lab -------------------------------------------------------------
# tap-pre :9001 in front of the running lab (start the lab with `make up MODE=B` first;
# --no-deps so APISIX is not re-rendered). Settings from .env or the command line:
#   TAP_PRE_UPSTREAM=maas (control path, skips APISIX)  TAP_PRE_MODE=canon|shuffle
#   TAP_PRE_SHUFFLE_TOOL_INDEX=-1  TAP_PRE_SHUFFLE_SEED=7  TAP_POST_MODE=canon (then make up MODE=B)
keyorder-up:
	mkdir -p logs/raw/pre logs/raw/post
	$(COMPOSE) --profile keyorder up -d --build --no-deps --force-recreate tap-pre
	@sleep 2; $(COMPOSE) --profile keyorder logs --tail 3 tap-pre

keyorder-down:
	$(COMPOSE) --profile keyorder rm -sf tap-pre

# Offline analyzer: make keyorder ARGS="--tap logs/tap --out reports/keyorder.md"
keyorder:
	python3 tap/keyorder.py $(ARGS)

test-keyorder:
	python3 tap/test_keyorder.py
