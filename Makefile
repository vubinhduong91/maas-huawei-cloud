SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: up down probe smoke render-check logs clean-logs

# Call MaaS directly to verify auth header / count_tokens / model names.
probe:
	bash scripts/probe-maas.sh

up:
	mkdir -p logs/apisix logs/tap reports
	chmod 777 logs/apisix  # written by the non-root apisix user inside the container
	$(COMPOSE) up -d --force-recreate

# -v also drops the volume holding the rendered config (it contains MAAS_API_KEY).
down:
	$(COMPOSE) down -v

smoke:
	bash scripts/smoke.sh

# Print the rendered apisix.yaml with all keys masked.
render-check:
	$(COMPOSE) run --rm --no-deps apisix-render python /src/render.py --redacted --out -

logs:
	$(COMPOSE) logs -f apisix

clean-logs:
	rm -rf logs/apisix/* logs/tap/*
