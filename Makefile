PORT ?= 8765
UI_PORT ?= 8501

.PHONY: install dev api ui test test-slow fixtures bench

install:
	uv sync

api:
	uv run uvicorn backend.app:app --host 127.0.0.1 --port $(PORT)

ui:
	SCRUB_API=http://127.0.0.1:$(PORT) uv run streamlit run frontend/app.py --server.address 127.0.0.1 --server.port $(UI_PORT) --browser.gatherUsageStats false

# start both; Ctrl-C stops both
dev:
	@trap 'kill 0' INT TERM; $(MAKE) api & $(MAKE) ui & wait

fixtures:
	uv run python scripts/make_fixtures.py

test:
	uv run pytest -q

test-slow:
	uv run pytest -q -m slow

bench:
	uv run python scripts/bench.py
