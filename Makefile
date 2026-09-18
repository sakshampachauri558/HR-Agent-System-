.PHONY: up down build rebuild logs ps seed smoke probe fmt tsc reset offline health

# Bring the stack up (dev-shaped: bind mounts, hot reload, exposed ports).
up:
	docker compose up -d --build

down:
	docker compose down

build:
	docker compose build

# Full rebuild, no layer cache, then start.
rebuild:
	docker compose build --no-cache
	docker compose up -d

logs:
	docker compose logs -f

ps:
	docker compose ps

# Loads seed/evaluations.json etc. — zero LLM request spend (see PRD §10).
seed:
	docker compose exec backend python -m scripts.seed

smoke:
	docker compose exec backend python -m scripts.smoke

probe:
	docker compose exec backend python -m scripts.smoke --probe

fmt:
	docker compose exec backend ruff check --fix .
	docker compose exec backend ruff format .

tsc:
	docker compose exec frontend npx tsc -b

# Nukes the pgdata volume too — the documented way to get back to a clean
# first-boot state (db/init.sql re-applies).
reset:
	docker compose down -v
	docker compose up -d --build

# Zero-key, zero-network offline path: start ollama, pull the model, then
# repoint the backend at it. No API key required anywhere in this path.
offline:
	docker compose --profile offline up -d ollama
	docker compose exec ollama ollama pull qwen2.5:7b-instruct
	LLM_PROVIDER=ollama docker compose up -d backend

health:
	curl -s http://localhost:$${BACKEND_PORT:-8000}/api/health | jq .
