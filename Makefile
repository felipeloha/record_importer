.PHONY: check lint format test live
unexport VIRTUAL_ENV

check:
	uv run ruff check
	uv run ruff format --check
	uv run mypy src
	uv run pytest -m "not live" --cov

format:
	uv run ruff format
	uv run ruff check --fix

test:
	uv run pytest -m "not live"

live:
	uv run pytest -m live
