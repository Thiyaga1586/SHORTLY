.PHONY: check-env config certs up register down logs ps

REQUIRED_ENV := ADMIN_API_KEY POSTGRES_PASSWORD SHORTLY_DB_PASSWORD REDIS_PASSWORD

check-env:
	@test -f .env || { echo "ERROR: .env is missing. Copy .env.example to .env and set the required secrets."; exit 1; }
	@set -eu; \
	for key in $(REQUIRED_ENV); do \
		value="$$(sed -n "s/^$${key}=//p" .env | tail -n 1)"; \
		if [ -z "$$value" ]; then echo "ERROR: $$key is empty in .env"; exit 1; fi; \
		case "$$value" in changeme*|postgres_secret|shortly_secret) echo "ERROR: $$key still uses an insecure legacy/default value"; exit 1;; esac; \
	done
	@docker compose config >/dev/null

config: check-env
	@docker compose config

certs: check-env
	@if [ ! -s certs/server.crt ] || [ ! -s certs/server.key ]; then \
		domain="$$(sed -n 's/^TLS_DOMAIN=//p' .env | tail -n 1)"; \
		bash scripts/gen_certs.sh "$${domain:-localhost}"; \
	fi

up: certs
	docker compose up -d --build
	bash scripts/register_nodes.sh

register: check-env
	bash scripts/register_nodes.sh

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps
