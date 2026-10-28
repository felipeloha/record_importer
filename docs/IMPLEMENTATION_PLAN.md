# crate2discogs — Implementation Plan

Read `REQUIREMENTS.md` first. This plan is written for Claude Code to execute **one step at a time**.

**Flow:** `import → identify → review → add-to-collection`

## Working rules (for Claude Code)

1. **TDD.** For each step: write the tests listed, watch them fail, implement, make them pass.
2. **One step per git commit.** Commit message: `step N: <title>`. Don't start step N+1 until `make check` is green.
3. **No network in unit tests.** Mock Discogs HTTP with `respx`, Ollama with `respx`, and `claude` with a fake executable on `PATH`. Tests that need real services are marked `@pytest.mark.live` and excluded by default.
4. **Stop and ask** when a step's "Done when" can't be met, or when an external API behaves differently from the requirements. Don't silently change scope.
5. Keep modules small and pure where possible: I/O at the edges, logic in testable functions.

## Naming

| Concept | CLI | Module | Notes |
|---|---|---|---|
| Read photos into the DB | `import` | `importer.py` | `import` is a Python keyword, so register the Typer command with `name="import"` |
| Decode, extract, match, resolve | `identify` | `pipeline.py` | |
| Human check | `review` | `review/app.py` | |
| Write to Discogs | `add-to-collection` | `add_to_collection.py` | The only command that writes to Discogs |

Photo stages: `imported → decoded → extracted | extract_failed → matched → resolved → added`.
Outcomes: `auto | review | not_found | skip`.

## Stack

- Python 3.12, `uv`, `typer` (CLI), `pydantic` v2 (models/config), `httpx` (HTTP), `respx` (HTTP mocks), `pytest`, `pytest-cov`, `ruff`, `mypy`
- `sqlite3` (stdlib) with a thin repository layer, **no ORM**
- `zxing-cpp` (barcodes), `Pillow` + `pillow-heif` (images), `rapidfuzz` (fuzzy matching), `fastapi` + `uvicorn` (review UI), `python-barcode` (dev only, to generate synthetic barcode images)

## Layout

```
crate2discogs/
  pyproject.toml  Makefile  .env.example
  src/crate2discogs/
    cli.py  config.py  db.py  models.py
    importer.py  barcodes.py
    extract/  base.py  ollama.py  claude_code.py  prompt.py
    normalize.py  discogs.py  ratelimit.py  search.py  scoring.py
    pipeline.py  dedupe.py  review/app.py  review/templates/  add_to_collection.py  report.py
  tests/
    unit/  integration/  live/
    fixtures/photos/  fixtures/expected.yaml  fixtures/discogs/*.json
```

`Makefile`: `make check` = `ruff check && ruff format --check && mypy src && pytest -m "not live" --cov`.

---

## Step 0 — Skeleton and config

**Build:** uv project, package layout, Typer app with `--help`, `config.py` (pydantic-settings reading env/.env), `Makefile`, `.env.example`.

**Tests (`tests/unit/test_config.py`):**
- Loads all values from env; defaults applied (`DISCOGS_FOLDER_ID=1`, `EXTRACTOR=ollama`, `OLLAMA_MODEL=qwen2.5vl:7b`).
- Missing `DISCOGS_TOKEN` → clear error naming the variable.
- `DISCOGS_FOLDER_ID=0` → validation error (folder must be non-zero).
- `EXTRACTOR=claude-code` with `ANTHROPIC_API_KEY` set → warning that it will bill the API instead of the subscription.
- `crate2discogs --help` exits 0 and lists every command from FR-10 (`init, import, identify, review, add-to-collection, status, export, eval`; stubs allowed).

**Done when:** `make check` is green.

---

## Step 1 — Database schema and repository

**Build:** `db.py` with migrations (a `user_version` pragma). Tables:
- `photos(id, sha256 UNIQUE, path, work_path, stage, created_at, updated_at)`. Stages: `imported → decoded → extracted | extract_failed → matched → resolved → added`.
- `barcodes(photo_id, symbology, value, valid)`
- `extractions(photo_id, extractor, model, raw_json, parsed_json, error)`
- `candidates(photo_id, release_id, rank, score, source_query, payload_json)`
- `resolutions(photo_id, outcome, release_id, copies, resolved_by: auto|user, resolved_at)`
- `collection_adds(release_id, instance_id, folder_id, added_at)`
- `http_cache(key PRIMARY KEY, status, body, fetched_at)`

**Tests:**
- Fresh DB migrates to the latest version; migrating twice is a no-op.
- Inserting the same sha256 twice → one row (upsert).
- Stage transition helper rejects illegal jumps (e.g. `imported → added`).
- Repository round-trips each table (insert → read equals).

---

## Step 2 — Import

**Build:** `importer.py` + the `import` command: scans a dir, filters extensions, hashes, applies EXIF orientation, converts HEIC → JPEG work copy, downscales to `MAX_EDGE` (default 1600), stores paths.

**Tests:**
- Dir with `a.jpg, b.HEIC, c.png, notes.txt` → 3 imported, txt ignored. Generate the images in `tmp_path`; use `pillow-heif` to write a tiny HEIC.
- Same file under two names → 1 photo (sha dedupe).
- Re-running import on the same dir → 0 new rows.
- Image with EXIF orientation 6 → work copy is rotated (assert width/height swapped).
- 4000×3000 input → work copy longest edge == 1600; original untouched.
- `--recursive` finds nested files; without it they are skipped.
- `crate2discogs import <dir>` works from the CLI (Typer `CliRunner`), despite `import` being a Python keyword.

---

## Step 3 — Barcode decoding

**Build:** `barcodes.py`: `ean_checksum_valid(digits) -> bool`; `decode(path) -> list[Barcode]` using zxing-cpp; keep only EAN-13/EAN-8/UPC-A/UPC-E with a valid checksum (R-1). Pipeline stage `decode`.

**Tests:**
- Checksum: `4250101497384` ✓, `4036909000014` ✓, `4250101497385` ✗, `0123` ✗, UPC-A `036000291452` ✓.
- Synthetic EAN-13 image (generated with `python-barcode`) → decoded value matches, `valid=True`.
- Synthetic Code128 image → returned with `valid=False` and **never** used as an identifier (R-1, AC-2).
- Image with no barcode → empty list, no exception.
- Rotated 90° synthetic EAN → still decoded.
- `@live`/fixture: the Kompakt photo → `4250101497384`; the BXR label photo → `4036909000014` (AC-1). The shop-sticker photos → no valid EAN.

---

## Step 4 — Normalisation utilities

**Build:** `normalize.py`:
- `catno_key(s)`: uppercase, strip spaces/dots/hyphens/slashes.
- `catno_variants(s, cap=6)`: OCR swaps (`O↔0, I↔1, S↔5, B↔8`) **only** at positions adjacent to a digit, ordered by number of swaps, deduped, capped.
- `strip_format_hints(title) -> (clean_title, hints, disc_count)`.
- `text_key(s)`: casefold, strip accents, `&`→`and`, collapse whitespace, drop punctuation.

**Tests (table-driven):**
- `catno_key("BXRCG 003-12") == "BXRCG00312"`; `catno_key("M.PM21") == "MPM21"`; `catno_key("DKMNTL 031") == "DKMNTL031"`.
- `"LOG016RE" in catno_variants("LOGO16RE")`.
- `catno_variants("PERLON100")` does **not** turn the `O` in `PERLON` into `0` unless it is adjacent to a digit; assert the cap is respected.
- `strip_format_hints("BASICTONALVOCABULARY (2XLP)")` → `("BASICTONALVOCABULARY", ["2XLP"], 2)`; `"IN WOOD, THAT (VINYL ONLY)"` → hint `"Vinyl Only"`, `disc_count=None`; `"PARADYGM SHIFT (2LP)"` → `disc_count=2`.
- `text_key("Mario Piu' & Mauro Picotto") == text_key("MARIO PIÙ and MAURO PICOTTO")`.

---

## Step 5 — Extraction models and Ollama extractor

**Build:**
- `models.py`: `Extraction` pydantic model matching FR-3 (all nullable). Export its JSON schema.
- `extract/base.py`: `Extractor` protocol `extract(image_path) -> Extraction`.
- `extract/prompt.py`: prompt explaining the photo types, R-1/R-2 (ignore shop barcodes and shop codes), "copy catno exactly as printed", and "return null rather than guess".
- `extract/ollama.py`: `POST /api/chat` with base64 image and `format=<schema>`, `temperature=0`, timeout, one retry on invalid JSON with a stricter suffix. Pipeline stage `extract` (skips photos with a valid EAN unless `--always-extract`).

**Tests (respx-mocked Ollama):**
- The request body contains the model, the base64 image, the schema in `format`, and `temperature: 0`.
- Valid JSON response → parsed `Extraction`; title format hints are post-processed through `strip_format_hints`.
- First response invalid JSON, second valid → success, 2 calls.
- Two invalid responses → stage `extract_failed`, error stored, pipeline continues to the next photo.
- Ollama down (connection error) → clear error message: "is `ollama serve` running?"
- Photo with a valid EAN is skipped by default and extracted with `--always-extract`.

---

## Step 6 — Fixture set and extraction eval

**Build:** copy the 10 test photos to `tests/fixtures/photos/`. Write `expected.yaml` (one entry per file: `photo_type`, `artist`, `title`, `label`, `catno`, `ean`, `disc_count`, `expected_release_id` filled in by hand after checking Discogs, `duplicate_of`). Starting values as read from the photos (**verify each one against the photo and Discogs**):

| Photo | type | artist | title | label | catno | ean | notes |
|---|---|---|---|---|---|---|---|
| Margaret Dygas sticker | shop_sticker | Margaret Dygas | In Wood, That | Perlon | Perlon100 | – | hint Vinyl Only |
| BXR centre label | center_label | Mario Più & Mauro Picotto | Imperiale | BXR Club | BXRCG 003-12 | 4036909000014 | ℗1999 Media Records GmbH |
| Kompakt barcode | ean | – | – | Kompakt | – | 4250101497384 | blurry, digits legible |
| Surgeon sticker #1 | shop_sticker | Surgeon | Basictonalvocabulary | Tresor | as printed (verify) | – | 2XLP |
| Surgeon sticker #2 | shop_sticker | same | same | same | same | – | duplicate_of #1 |
| Robert Hood sticker | shop_sticker | Robert Hood | Apartement Zero | Logistic | LOG016RE (printed `LOGO16RE`?) | – | OCR trap |
| M-Plant back cover | back_cover | Robert Hood | (not printed) | M-Plant | MPM16LP | – | 2013, Belgium, 8 tracks A1–D2 |
| M-Plant centre label | center_label | – | 20 Years M-Plant Music | M-Plant | M.PM21 | – | |
| Paradygm Shift Vol 1 | shop_sticker | Robert Hood | Paradygm Shift - Volume 1 | Dekmantel | DKMNTL 031 | – | |
| Paradygm Shift 2LP | shop_sticker | Robert Hood | Paradygm Shift | Dekmantel | DKMNTL 050 | – | 2LP |

`eval` command: run extraction (or read cached extractions) and print per-field accuracy using `catno_key` / `text_key` comparisons.

**Tests:**
- `expected.yaml` loads and validates against a schema; every file it references exists.
- Eval scoring function: synthetic predicted-vs-expected pairs → the correct per-field accuracy numbers, including null handling (expected null and predicted null counts as correct).
- `@live`: `eval` with the Ollama extractor reports catno accuracy ≥ 90% (AC-3). **If it misses, stop and report per-photo failures before tuning the prompt or swapping the model.**

---

## Step 7 — Discogs client, rate limiter, cache

**Build:** `ratelimit.py` (token bucket, injectable clock), `discogs.py` (httpx; auth + User-Agent headers; `search(**params)`, `release(id)`, `collection_instances(release_id)`, `add_release(folder_id, release_id)`; retries on 429/5xx with exponential backoff; reads `X-Discogs-Ratelimit-Remaining`; GET responses cached in `http_cache`).

**Tests (respx + fake clock):**
- Every request has `Authorization: Discogs token=…` and the configured `User-Agent`.
- 61 requests within a minute → the 61st waits (assert on the fake clock, no real sleeping).
- `X-Discogs-Ratelimit-Remaining: 0` → the client pauses before the next call.
- 429 then 200 → success after backoff; three 5xx → raises with context.
- The same search twice → one HTTP call (cache hit); an expired TTL → refetch.
- POST is never cached.
- `add_release` builds the correct path and returns the `instance_id` from a JSON fixture.

---

## Step 8 — Search cascade

**Build:** `search.py`: given an `Extraction` + barcodes → an ordered list of query param dicts per FR-4, executed until an auto-acceptable result appears or the budget (default 8) runs out. Every query includes `type=release, format=Vinyl`. All candidates are stored with their `source_query`. Pipeline stage `match`.

**Tests (pure, with a fake client):**
- Valid EAN present → first query is `{barcode: …}`.
- No EAN, catno + label → first query is `{catno, label}`; on empty results, the variant queries follow in order.
- Only artist + title → `{artist, release_title}`, then `q`.
- The budget is respected (budget=3 → at most 3 calls).
- Short-circuit: an auto-acceptable result on query 1 → no further queries.
- The shop code is never present in any query (R-2).
- `ean_digits` from the extraction is used only if its checksum is valid.

---

## Step 9 — Scoring and resolution

**Build:** `scoring.py`: `score(candidate, extraction, barcodes) -> float` with weighted features (barcode, catno key, label, artist, title, disc_count, year, country) and `resolve(candidates) -> auto | review | not_found`. For ties, optionally fetch `release(id)` for the top 2–3 to compare `formats[].qty` with `disc_count`. Weights and thresholds come from config. Pipeline stage `resolve`.

**Tests (table-driven, synthetic candidates from JSON fixtures):**
- Single barcode-exact hit → `auto`.
- Catno + label exact, single result → `auto`.
- Two pressings with the same catno, one a 2LP, extraction `disc_count=2` → `auto` for the 2LP.
- Two pressings indistinguishable → `review`, both in the top 3.
- Only a fuzzy artist/title match → `review`.
- No candidates → `not_found`.
- Monotonicity: adding a matching field never lowers the score.
- Regression guard: a candidate whose catno differs (by key) but whose artist/title match is **never** `auto` (protects AC-5 "zero wrong autos").

---

## Step 10 — `identify`: pipeline orchestration and resume

**Build:** `pipeline.py` + the `identify` command: stages decode → extract → match → resolve, each selecting only photos at the right stage, saving to the DB per photo, with a progress bar. Supports `--stage` and `--limit`.

**Tests (integration, everything mocked):**
- 5 photos identified end to end → all reach `resolved`.
- Inject an exception on photo 3 during extract, then re-run → photos 1–2 not re-extracted (call counter), 3–5 done (AC-7, first half).
- `--limit 2` processes exactly 2.
- `--stage match` doesn't call the extractor.
- `status` output matches the DB counts.

---

## Step 11 — Deduplication

**Build:** `dedupe.py` groups resolved photos by `release_id` and exposes groups with a `copies` field (default 1).

**Tests:**
- Two photos → same release → one group, `copies=1`, both photo IDs listed (AC-4).
- The user sets `copies=2` → the add plan contains 2 adds for that release.
- Different releases → separate groups.
- A photo with outcome `skip` is excluded.

---

## Step 12 — Review UI

**Build:** FastAPI app bound to `127.0.0.1`, server-rendered HTML (Jinja2, no JS framework). Pages: queue (`review` + `not_found` first, `auto` collapsed), item detail (photo, extracted fields, top-3 candidates with thumbnail, catno, label, year, country, format, Discogs link). Actions: pick a candidate, paste a release ID or URL, skip, set copies, override an auto. The `review` command opens the browser.

**Tests (FastAPI `TestClient`, mocked Discogs):**
- The queue lists only `review` and `not_found` items in the main section; `auto` items appear in the collapsed section.
- POST pick → the resolution is stored with `resolved_by=user`.
- POST a pasted URL `https://www.discogs.com/release/123-Foo` → parses `123`, validates via `release(123)` (mocked); an invalid ID → error message, no write.
- POST skip → excluded from the add plan.
- Overriding an auto works and is persisted.
- The server binds to localhost only (assert the config).

---

## Step 13 — Add to collection

**Build:** `add_to_collection.py` + the `add-to-collection` command: builds the add plan from the dedupe groups, dry run by default (prints the plan table), `--apply` checks existing instances then POSTs, stores `instance_id` in `collection_adds`, moves photos to stage `added`, resumable.

**Tests (respx):**
- Dry run → zero POST calls (AC-6); the plan lists every release with copies.
- `--apply` with a release already in the collection (1 instance) and `copies=1` → skip; with `copies=2` → one POST.
- A failure on item 3 of 5, then re-run → items 1–2 are not re-posted (AC-7, second half).
- The folder ID from config is used in the path.
- The summary reports added / skipped / failed.

---

## Step 14 — Claude Code extractor (optional)

**Build:** `extract/claude_code.py` runs `claude -p <prompt with image path> --output-format json` (verify the flags in the current Claude Code docs) via `subprocess`, with a timeout, parses the result into `Extraction`, and refuses to run if `ANTHROPIC_API_KEY` is set unless `--allow-api-billing`.

**Tests (fake `claude` script on `PATH` in `tmp_path`):**
- The fake prints valid JSON → parsed `Extraction`.
- The fake prints garbage → one retry, then `extract_failed`.
- The fake exits non-zero → error captured with its stderr.
- A timeout → `extract_failed` with a timeout message.
- `ANTHROPIC_API_KEY` set → refuses with a clear message; `--allow-api-billing` overrides.
- `@live`: `eval` with `EXTRACTOR=claude-code` on the fixture set; compare its accuracy with Ollama.

---

## Step 15 — Export and report

**Build:** the `export --csv` command and the end-of-run summary (counts per outcome, top failure reasons, estimated review workload).

**Tests:**
- The CSV has a header and one row per photo; Discogs URL is formatted `https://www.discogs.com/release/<id>`.
- Unicode survives the round-trip (`Più`).
- The summary counts match the DB.

---

## Step 16 — End-to-end acceptance on the fixture set (live, manual)

1. `ollama pull qwen2.5vl:7b && ollama serve`
2. `crate2discogs init && crate2discogs import tests/fixtures/photos && crate2discogs identify`
3. Check AC-1…AC-5 using `crate2discogs eval --expected tests/fixtures/expected.yaml`
4. `crate2discogs review`: resolve the remaining items.
5. `crate2discogs add-to-collection` (dry run), check the plan, then `add-to-collection --apply` to a **test folder** (create a "crate2discogs-test" folder on Discogs and set `DISCOGS_FOLDER_ID`).
6. Kill an `identify` run halfway on a copy of the set, re-run, and confirm AC-7.

**Done when:** all of AC-1…AC-8 hold. Then run it on the first 50 real records, tune the thresholds from the review rate, and do the rest.
