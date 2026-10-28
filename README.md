# crate2discogs

Photos of vinyl records → exact releases in your Discogs collection. See `docs/REQUIREMENTS.md` and `docs/IMPLEMENTATION_PLAN.md`.

## Setup

```
brew install uv ollama
ollama serve                               # native, not Docker (Apple GPU); keep it running
ollama pull qwen2.5vl:7b
uv sync                                    # creates .venv/ in the repo and installs everything
cp .env.example .env                       # fill DISCOGS_TOKEN, DISCOGS_USERNAME, DISCOGS_FOLDER_ID
uv run crate2discogs init                  # checks token, folder, model
```

## Python environment

The project uses its own virtual environment in `.venv/`, created by `uv sync` (Python version from `.python-version`).

- No activation needed: `uv run <command>` always uses `.venv/`.
- To use the commands directly, activate it in the current shell (it must be sourced, not executed):

  ```
  source .venv/bin/activate
  crate2discogs --help
  deactivate                               # leave the environment
  ```
- If another environment is active (e.g. pyenv sets `VIRTUAL_ENV`), `uv` prints a warning and still uses `.venv/`.
- After pulling changes to `pyproject.toml` or `uv.lock`: run `uv sync` again.
- Configuration is read from `.env` in the current directory (or from environment variables), so run commands from the repo root.

## Use

```
uv run crate2discogs import ~/Pictures/crate
uv run crate2discogs identify              # resumable; re-run after a crash
uv run crate2discogs review                # browser UI on 127.0.0.1
uv run crate2discogs add-to-collection     # dry run
uv run crate2discogs add-to-collection --apply
uv run crate2discogs status
uv run crate2discogs export --csv out.csv
uv run crate2discogs identify --force      # search Discogs again for everything not yet added
uv run crate2discogs reset                 # delete DB, work copies and log; asks first
```

- User choices in review always win. `identify --force` re-searches and re-scores everything else; `identify --stage resolve --force` only re-scores (no Discogs calls).
- `reset` keeps your photos, `.env` and code. After it, start again with `import`.
- `copies` per release = total copies wanted in the collection; only the missing ones are added.

## Develop

```
make check                                 # ruff, mypy, offline tests
uv run crate2discogs eval --expected tests/fixtures/expected.yaml [--extract-only]
make live                                  # tests that need Ollama/Discogs
```
