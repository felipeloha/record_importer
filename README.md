# crate2discogs

Photos of vinyl records → exact releases in your Discogs collection. See `docs/REQUIREMENTS.md` and `docs/IMPLEMENTATION_PLAN.md`.

## Setup

```
brew install ollama && ollama serve        # native, not Docker (Apple GPU)
ollama pull qwen2.5vl:7b
uv sync
cp .env.example .env                       # fill DISCOGS_TOKEN, DISCOGS_USERNAME, DISCOGS_FOLDER_ID
uv run crate2discogs init                  # checks token, folder, model
```

## Use

```
uv run crate2discogs import ~/Pictures/crate
uv run crate2discogs identify              # resumable; re-run after a crash
uv run crate2discogs review                # browser UI on 127.0.0.1
uv run crate2discogs add-to-collection     # dry run
uv run crate2discogs add-to-collection --apply
uv run crate2discogs status
uv run crate2discogs export --csv out.csv
```

- User choices in review always win; `identify --stage resolve --force` re-scores everything else (no Discogs calls).
- `copies` per release = total copies wanted in the collection; only the missing ones are added.

## Develop

```
make check                                 # ruff, mypy, offline tests
uv run crate2discogs eval --expected tests/fixtures/expected.yaml [--extract-only]
make live                                  # tests that need Ollama/Discogs
```
