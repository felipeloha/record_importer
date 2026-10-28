# crate2discogs — Requirements

A local CLI that turns phone photos of ~500 vinyl records into entries in my Discogs collection, with a human review step for anything ambiguous.

**Flow:** `import → identify → review → add-to-collection`

## 1. Goal

- Input: a folder of phone photos, roughly **one photo per record**.
- Output: every record added to my Discogs collection as the **exact release/pressing**, not just the album.
- Optimise for my time: shooting ~5 s per record, then batch processing, then reviewing only the uncertain ones.
- No paid API key required. Image recognition runs **locally** (Ollama), with Claude Code (on my existing subscription) as an optional alternative extractor.

## 2. Input photo types

Every photo contains at least one strong identifier. The pipeline must handle all of these:

| Type | Description | Primary identifier |
|---|---|---|
| `ean` | Printed EAN/UPC barcode on sleeve or label (digits usually printed underneath) | EAN-13 / UPC-A |
| `shop_sticker` | Record-shop price sticker: `ARTIST / TITLE (format hint) / Label / CATNO` + an **internal shop barcode** + a shop code like `c3f - ic+` | Catno + label from the text |
| `center_label` | Photo of the record's centre label | Catno, label, artist, title, year |
| `back_cover` | Back of sleeve with credits/tracklist | Catno, label, year, tracklist, country |
| `other` | Anything else (front cover only, etc.) | Artist/title, low confidence |

### Hard rules derived from real photos
- **R-1** The shop-sticker barcode is **not** an EAN. Only EAN-13, EAN-8, UPC-A and UPC-E codes with a **valid checksum** may be used as barcodes. Code128 and other symbologies are ignored.
- **R-2** Shop codes such as `c3f - ic+`, `bw7 - j6*` must never be used as identifiers.
- **R-3** Format hints in titles (`(2LP)`, `(2XLP)`, `(VINYL ONLY)`) must be stripped from the title and kept as structured hints (e.g. `disc_count=2`).
- **R-4** OCR confusions in catalog numbers must be tolerated: `O↔0`, `I↔1`, `S↔5`, `B↔8`, and spaces, dots and hyphens (`LOGO16RE` vs `LOG016RE`, `M.PM21` vs `MPM21`, `BXRCG 003-12`).
- **R-5** Photos may be rotated (EXIF orientation) and may be HEIC (iPhone default).
- **R-6** The same record may be photographed more than once. Duplicates are resolved by Discogs release ID, not by photo grouping.

## 3. Functional requirements

### FR-1 Import
- Scan a directory (non-recursive by default, `--recursive` optional) for `.jpg .jpeg .png .heic .heif`.
- Identify each photo by SHA-256 of its file content; re-running import must not create duplicates.
- Apply the EXIF orientation. Keep the original; create a working copy downscaled to a configurable longest edge (default 1600 px) for the vision model. The working copy is always JPEG (also used by the review UI, since browsers can't show HEIC).

### FR-2 Barcode decoding (local, deterministic)
- Decode barcodes locally before any AI call, on the full-resolution original with EXIF orientation applied (downscaling loses barcode detail).
- Accept only the symbologies and checksum rule from R-1.
- If a valid EAN/UPC is found, the vision step is optional for that photo (configurable: `--always-extract`).

### FR-3 Vision extraction
- A pluggable `Extractor` interface with two implementations:
  - `ollama` (default): local vision model through the Ollama HTTP API, using **structured JSON output** (JSON schema). The model is configurable; default `qwen2.5vl:7b`.
  - `claude-code` (optional): calls the `claude` CLI in headless mode with the image path. Uses my subscription. If `ANTHROPIC_API_KEY` is set, it would bill the API instead, so the extractor refuses to run unless `--allow-api-billing` is passed.
- Output schema per photo (all fields nullable):
  - `photo_type`: one of the types above
  - `artist`, `title`, `label`, `catno` (exactly as printed), `catno_candidates` (list)
  - `ean_digits` (digits printed under a barcode, if legible)
  - `format_hints` (list: `"2LP"`, `"12\""`, `"Vinyl Only"`, …), `disc_count`
  - `year`, `country`, `tracklist` (list of `{position, title}`)
  - `shop_code` (captured only so it can be ignored)
  - `confidence` (0–1, model self-report, advisory only)
- Invalid JSON gets one retry with a stricter prompt. After that, mark the photo `extract_failed` and continue.

### FR-4 Discogs search cascade
Queries run in order and stop as soon as the candidates found so far give an `auto` outcome (FR-5):
1. `barcode=<EAN>` (from the decoder, otherwise from `ean_digits` if the checksum is valid)
2. `catno=<catno>` + `label=<label>`
3. `catno=<variant>` for each catno variant (capped, default 6)
4. `artist=<artist>` + `release_title=<title>` (+ `label` if known)
5. `q="<artist> <title>"` as a last resort

- All searches use `type=release` and `format=Vinyl`.
- Queries send the catno as printed (step 2) or as an OCR variant that keeps the printed spacing and punctuation (step 3). The normalised `catno_key` is only used to compare results, never as a query.
- Per-photo query budget: default 8 Discogs calls.

### FR-5 Scoring and outcome
Each candidate gets a score from field agreement. The score only ranks candidates:
- barcode exact match (strongest; Discogs barcodes are compared digits-only)
- normalised catno exact match
- label match (fuzzy)
- artist and title match (fuzzy, accent- and case-insensitive)
- `disc_count` vs Discogs format quantity, year, country (tie-breakers)

Outcome per photo, decided by two rules:
- `auto`: the top candidate **(a)** matches on barcode or normalised catno, **and (b)** is ahead of the runner-up by at least `AUTO_MARGIN` (or there is no runner-up). Both must hold.
- `review`: candidates exist but the top one fails (a) or (b). Typical cases: several pressings share the barcode or catno, or only artist/title matched.
- `not_found`: no candidates.
- `skip`: set only by the user in review.

`AUTO_MARGIN` is a config value, tuned on the fixture set (see §6).

### FR-6 Deduplication and copies
- Photos that resolve to the same `release_id` form one group.
- Each group has one number: `copies` = how many copies of this release I want in my collection in total. Default 1.
- `add-to-collection` adds `copies − instances already in the collection`. If that is ≤ 0, nothing is added.

### FR-7 Review UI
- `crate2discogs review` starts a local web server (localhost only) and opens the browser.
- The main queue contains:
  - every `review` and `not_found` photo
  - every group with more than one photo, even if `auto`, with the question "same record photographed twice, or N copies?"
- Each item shows: the photo, the extracted fields, and the top 3 candidates (thumbnail, artist – title, label, catno, year, country, format, link to Discogs).
- Actions: pick a candidate, paste a Discogs release ID or URL (validated against the API; `/master/` URLs are rejected), mark as skip, set `copies`.
- Auto-accepted single photos are listed in a collapsed section and can be overridden.
- Choices persist immediately (closing the browser loses nothing).
- Photos are served by photo ID only (never by file path), from the JPEG working copy.

### FR-8 Add to collection
- `crate2discogs add-to-collection` is a **dry run by default**; `--apply` performs the writes. This is the **only** command that writes to Discogs.
- The plan shows one row per release: wanted `copies`, instances already in the collection, number to add.
- The "already in the collection" lookup always goes to Discogs (never cached), so re-runs see adds made by earlier runs.
- Add to a configurable folder (default `1` = Uncategorized; the folder ID must be non-zero).
- Store the returned instance ID. Re-running never double-adds.

### Conflict rule
User choices always win. Re-running `identify` (including `--force`) never overwrites a resolution set in review (`resolved_by=user`). To redo a user choice, change it in review.

### FR-9 Status, export, eval
- `status`: counts per stage and outcome.
- `export --csv out.csv`: one row per photo (path, stage, outcome, release_id, score, extracted fields, Discogs URL).
- `eval --expected tests/fixtures/expected.yaml`: per-field extraction accuracy and end-to-end outcome accuracy on a labelled set.

### FR-10 CLI surface
```
crate2discogs init                          # create db + check config, Discogs token, folder, Ollama model
crate2discogs import <dir> [--recursive]
crate2discogs identify [--stage decode|extract|match|resolve] [--limit N] [--always-extract]
                       [--force] [--allow-api-billing]
crate2discogs review
crate2discogs add-to-collection [--apply]
crate2discogs status
crate2discogs export --csv <file>
crate2discogs eval --expected <yaml>        # dev: measure accuracy on the fixture set
```
- `identify` with no `--stage` runs every non-interactive stage in order and is resumable.
- `identify --stage resolve --force` re-scores photos from their stored candidates (no new Discogs searches), e.g. after changing `AUTO_MARGIN`. It skips user resolutions (see Conflict rule).
- `init` checks: config is valid, `GET /oauth/identity` returns `DISCOGS_USERNAME`, `DISCOGS_FOLDER_ID` exists, and the Ollama model is pulled (if `EXTRACTOR=ollama`).

Photo stages: `imported → decoded → extracted | extract_failed → matched → resolved → added`.
A photo moves to `added` once its group's adds are complete. Photos with outcome `review`, `not_found` or `skip` stay at `resolved`.

## 4. Non-functional requirements

- **NFR-1 Resumable and idempotent:** every stage persists to SQLite. A crash at photo 300 loses nothing; re-runs skip completed work.
- **NFR-2 Rate limiting:** Discogs allows 60 authenticated requests/min. Use a token bucket, honour the `X-Discogs-Ratelimit-Remaining` header, and back off exponentially on 429 and 5xx.
- **NFR-3 Caching:** cache Discogs search and release responses in SQLite keyed by URL+params (TTL configurable, default 30 days) so tuning thresholds doesn't re-hit the API. Collection endpoints are never cached.
- **NFR-4 Privacy:** with the `ollama` extractor, no image leaves the machine. Only text queries go to Discogs.
- **NFR-5 Configuration** via environment variables or `.env`:
  `DISCOGS_TOKEN` (required), `DISCOGS_USERNAME` (required), `DISCOGS_FOLDER_ID=1`, `EXTRACTOR=ollama|claude-code`, `OLLAMA_URL=http://localhost:11434`, `OLLAMA_MODEL=qwen2.5vl:7b`, `MAX_EDGE=1600`, `AUTO_MARGIN`, `DB_PATH=./crate.db`, `USER_AGENT=crate2discogs/0.1`.
- **NFR-6 Observability:** structured logs (one line per photo per stage), a progress bar, and a final summary.
- **NFR-7 Performance target:** 500 photos fully identified (decode + extract + match + resolve) in under 2 hours on an Apple Silicon MacBook with the local model. Stages run one after another, so their times add up:
  - Discogs at 60 req/min: ~20–30 min if most photos resolve within 2–3 queries; ~67 min worst case (8 queries per photo).
  - Extraction: ~10–20 s per photo is expected for a 7–8B vision model at 1600 px, i.e. 1.4–2.8 h. Measured on the fixture set in Step 6; if over budget, lower `MAX_EDGE` first.
- **NFR-8 Platform:** macOS on Apple Silicon. Python 3.12, managed with `uv`. Ollama runs **natively** (`brew install ollama`), not in Docker, because Docker on macOS has no Apple GPU access.

## 5. Out of scope (v1)
- Marketplace listings/inventory.
- Editing collection custom fields (condition, notes). v2 candidate: populate a notes field with the shop code or purchase info.
- Submitting new releases to Discogs.
- Mobile app or any shooting workflow beyond "photos in a folder".

## 6. Acceptance criteria

Fixture set: the 10 test photos (Margaret Dygas, Mario Più & Mauro Picotto, Kompakt EAN, Surgeon ×2, Robert Hood – Apartement Zero, Robert Hood M-Plant back cover, M-Plant 20 Years label, Robert Hood – Paradygm Shift Vol 1, Robert Hood – Paradygm Shift 2LP), labelled in `tests/fixtures/expected.yaml`.

- **AC-1** Both EANs are found locally with valid checksums: `4250101497384`, `4036909000014`. Source: the barcode decoder, or the printed digits (`ean_digits`) read by the local extractor if the barcode image is too blurry to decode.
- **AC-2** No shop-sticker barcode is ever used as an EAN.
- **AC-3** Catno extraction (after normalisation) is ≥ 90% correct on the fixture set.
- **AC-4** The two Surgeon photos resolve to one release and are added to the collection once.
- **AC-5** Every fixture photo ends in `auto` with the correct release, or in `review` with the correct release among the top 3. Zero wrong `auto` outcomes.
- **AC-6** `add-to-collection` without `--apply` performs zero write calls.
- **AC-7** Killing the process mid-`identify` or mid-`add-to-collection --apply` and re-running completes without duplicated work or duplicated collection entries.
- **AC-8** The whole unit test suite runs offline (no Discogs, no Ollama, no `claude`).

## 7. External API references (verify during implementation)
- Discogs API: https://www.discogs.com/developers
  - `GET /database/search` (params: `type, barcode, catno, label, artist, release_title, format, q, per_page`)
  - `GET /releases/{id}`
  - `GET /oauth/identity` (used by `init`)
  - `GET /users/{username}/collection/releases/{release_id}`
  - `POST /users/{username}/collection/folders/{folder_id}/releases/{release_id}`
  - Auth header: `Authorization: Discogs token=<token>`. A `User-Agent` is required.
- Ollama: `POST /api/chat` with `images` (base64) and `format` (JSON schema).
- Claude Code headless mode: `claude -p` with JSON output. The image directory must be readable (`--add-dir`). Check flags against the current Claude Code docs, including whether a JSON-schema output flag exists.
