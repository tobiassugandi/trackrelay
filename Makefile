UV := uv

.PHONY: sync test lint run

sync:
	$(UV) sync --locked --python 3.12

test:
	$(UV) run --locked pytest

lint:
	$(UV) run --locked ruff check .

run:
	$(UV) run --locked uvicorn trackrelay.main:app --reload --host 127.0.0.1 --port 8000
