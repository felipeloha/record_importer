import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Stage = Literal["imported", "decoded", "extracted", "extract_failed", "matched", "resolved", "added"]
Outcome = Literal["auto", "review", "not_found", "skip"]
ResolvedBy = Literal["auto", "user"]

STAGES: tuple[Stage, ...] = (
    "imported",
    "decoded",
    "extracted",
    "extract_failed",
    "matched",
    "resolved",
    "added",
)

# `resolved → matched/resolved` allow re-searching and re-scoring (identify --force).
_TRANSITIONS: dict[str, set[str]] = {
    "imported": {"decoded"},
    "decoded": {"extracted", "extract_failed"},
    "extract_failed": {"extracted", "extract_failed", "matched"},
    "extracted": {"matched"},
    "matched": {"matched", "resolved"},
    "resolved": {"matched", "resolved", "added"},
    "added": set(),
}

_MIGRATIONS: list[str] = [
    """
    CREATE TABLE photos (
        id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL,
        work_path TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'imported',
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX photos_stage ON photos(stage);
    CREATE TABLE barcodes (
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        symbology TEXT NOT NULL,
        value TEXT NOT NULL,
        valid INTEGER NOT NULL
    );
    CREATE INDEX barcodes_photo ON barcodes(photo_id);
    CREATE TABLE extractions (
        photo_id INTEGER PRIMARY KEY REFERENCES photos(id),
        extractor TEXT NOT NULL,
        model TEXT,
        raw_json TEXT,
        parsed_json TEXT,
        error TEXT
    );
    CREATE TABLE candidates (
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        release_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        score REAL NOT NULL,
        source_query TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX candidates_photo ON candidates(photo_id);
    CREATE TABLE resolutions (
        photo_id INTEGER PRIMARY KEY REFERENCES photos(id),
        outcome TEXT NOT NULL,
        release_id INTEGER,
        resolved_by TEXT NOT NULL,
        resolved_at TEXT NOT NULL
    );
    CREATE TABLE release_groups (
        release_id INTEGER PRIMARY KEY,
        copies INTEGER NOT NULL DEFAULT 1 CHECK (copies >= 1)
    );
    CREATE TABLE collection_adds (
        release_id INTEGER NOT NULL,
        instance_id INTEGER NOT NULL,
        folder_id INTEGER NOT NULL,
        added_at TEXT NOT NULL
    );
    CREATE TABLE http_cache (
        key TEXT PRIMARY KEY,
        status INTEGER NOT NULL,
        body TEXT NOT NULL,
        fetched_at REAL NOT NULL
    );
    """,
]

LATEST_VERSION = len(_MIGRATIONS)


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Photo:
    id: int
    sha256: str
    path: Path
    work_path: Path
    stage: Stage
    last_error: str | None


@dataclass(frozen=True)
class BarcodeRow:
    symbology: str
    value: str
    valid: bool


@dataclass(frozen=True)
class ExtractionRow:
    extractor: str
    model: str | None
    raw_json: str | None
    parsed_json: str | None
    error: str | None


@dataclass(frozen=True)
class CandidateRow:
    release_id: int
    rank: int
    score: float
    source_query: str
    payload_json: str


@dataclass(frozen=True)
class ResolutionRow:
    outcome: Outcome
    release_id: int | None
    resolved_by: ResolvedBy
    resolved_at: str = ""


@dataclass(frozen=True)
class CollectionAddRow:
    release_id: int
    instance_id: int
    folder_id: int
    added_at: str


@dataclass(frozen=True)
class CacheEntry:
    status: int
    body: str
    fetched_at: float


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def _photo(row: sqlite3.Row) -> Photo:
    return Photo(
        row["id"], row["sha256"], Path(row["path"]), Path(row["work_path"]), row["stage"], row["last_error"]
    )


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # photos

    def upsert_photo(self, sha256: str, path: Path, work_path: Path) -> tuple[int, bool]:
        """Insert a photo unless its sha256 exists. Returns (id, created)."""
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO photos (sha256, path, work_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(sha256) DO NOTHING",
            (sha256, str(path), str(work_path), now, now),
        )
        created = cur.rowcount == 1
        row = self.conn.execute("SELECT id FROM photos WHERE sha256 = ?", (sha256,)).fetchone()
        return row["id"], created

    def has_sha(self, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (sha256,)).fetchone() is not None

    def get_photo(self, photo_id: int) -> Photo | None:
        row = self.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return _photo(row) if row else None

    def all_photos(self) -> list[Photo]:
        return [_photo(r) for r in self.conn.execute("SELECT * FROM photos ORDER BY id")]

    def photos_at_stage(self, stage: Stage, limit: int | None = None) -> list[Photo]:
        rows = self.conn.execute(
            "SELECT * FROM photos WHERE stage = ? ORDER BY id LIMIT ?",
            (stage, -1 if limit is None else limit),
        )
        return [_photo(r) for r in rows]

    def count_photos(self) -> int:
        n: int = self.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        return n

    def set_stage(self, photo_id: int, stage: Stage) -> None:
        photo = self.get_photo(photo_id)
        if photo is None:
            raise KeyError(photo_id)
        if stage not in _TRANSITIONS[photo.stage]:
            raise IllegalTransition(f"photo {photo_id}: {photo.stage} → {stage}")
        self.conn.execute(
            "UPDATE photos SET stage = ?, updated_at = ? WHERE id = ?", (stage, _now(), photo_id)
        )

    def set_error(self, photo_id: int, error: str | None) -> None:
        self.conn.execute(
            "UPDATE photos SET last_error = ?, updated_at = ? WHERE id = ?", (error, _now(), photo_id)
        )

    # barcodes

    def replace_barcodes(self, photo_id: int, barcodes: list[BarcodeRow]) -> None:
        with self._tx():
            self.conn.execute("DELETE FROM barcodes WHERE photo_id = ?", (photo_id,))
            self.conn.executemany(
                "INSERT INTO barcodes (photo_id, symbology, value, valid) VALUES (?, ?, ?, ?)",
                [(photo_id, b.symbology, b.value, int(b.valid)) for b in barcodes],
            )

    def get_barcodes(self, photo_id: int) -> list[BarcodeRow]:
        rows = self.conn.execute(
            "SELECT symbology, value, valid FROM barcodes WHERE photo_id = ? ORDER BY rowid", (photo_id,)
        )
        return [BarcodeRow(r["symbology"], r["value"], bool(r["valid"])) for r in rows]

    # extractions

    def save_extraction(self, photo_id: int, e: ExtractionRow) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO extractions (photo_id, extractor, model, raw_json, parsed_json, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (photo_id, e.extractor, e.model, e.raw_json, e.parsed_json, e.error),
        )

    def get_extraction(self, photo_id: int) -> ExtractionRow | None:
        r = self.conn.execute("SELECT * FROM extractions WHERE photo_id = ?", (photo_id,)).fetchone()
        if r is None:
            return None
        return ExtractionRow(r["extractor"], r["model"], r["raw_json"], r["parsed_json"], r["error"])

    # candidates

    def replace_candidates(self, photo_id: int, candidates: list[CandidateRow]) -> None:
        with self._tx():
            self.conn.execute("DELETE FROM candidates WHERE photo_id = ?", (photo_id,))
            self.conn.executemany(
                "INSERT INTO candidates (photo_id, release_id, rank, score, source_query, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (photo_id, c.release_id, c.rank, c.score, c.source_query, c.payload_json)
                    for c in candidates
                ],
            )

    def get_candidates(self, photo_id: int) -> list[CandidateRow]:
        rows = self.conn.execute("SELECT * FROM candidates WHERE photo_id = ? ORDER BY rank", (photo_id,))
        return [
            CandidateRow(r["release_id"], r["rank"], r["score"], r["source_query"], r["payload_json"])
            for r in rows
        ]

    # resolutions

    def save_resolution(self, photo_id: int, res: ResolutionRow) -> bool:
        """Store a resolution. An automatic one never replaces a user one (conflict rule). Returns written."""
        if res.resolved_by == "auto":
            existing = self.get_resolution(photo_id)
            if existing is not None and existing.resolved_by == "user":
                return False
        self.conn.execute(
            "INSERT OR REPLACE INTO resolutions (photo_id, outcome, release_id, resolved_by, resolved_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (photo_id, res.outcome, res.release_id, res.resolved_by, _now()),
        )
        return True

    def delete_resolution(self, photo_id: int) -> None:
        self.conn.execute("DELETE FROM resolutions WHERE photo_id = ?", (photo_id,))

    def get_resolution(self, photo_id: int) -> ResolutionRow | None:
        r = self.conn.execute("SELECT * FROM resolutions WHERE photo_id = ?", (photo_id,)).fetchone()
        if r is None:
            return None
        return ResolutionRow(r["outcome"], r["release_id"], r["resolved_by"], r["resolved_at"])

    # release groups

    def get_copies(self, release_id: int) -> int:
        r = self.conn.execute(
            "SELECT copies FROM release_groups WHERE release_id = ?", (release_id,)
        ).fetchone()
        return int(r["copies"]) if r else 1

    def set_copies(self, release_id: int, copies: int) -> None:
        if copies < 1:
            raise ValueError("copies must be >= 1")
        self.conn.execute(
            "INSERT OR REPLACE INTO release_groups (release_id, copies) VALUES (?, ?)", (release_id, copies)
        )

    # collection adds

    def record_collection_add(self, release_id: int, instance_id: int, folder_id: int) -> None:
        self.conn.execute(
            "INSERT INTO collection_adds (release_id, instance_id, folder_id, added_at) VALUES (?, ?, ?, ?)",
            (release_id, instance_id, folder_id, _now()),
        )

    def collection_adds(self, release_id: int) -> list[CollectionAddRow]:
        rows = self.conn.execute(
            "SELECT * FROM collection_adds WHERE release_id = ? ORDER BY rowid", (release_id,)
        )
        return [
            CollectionAddRow(r["release_id"], r["instance_id"], r["folder_id"], r["added_at"]) for r in rows
        ]

    # http cache

    def cache_get(self, key: str) -> CacheEntry | None:
        r = self.conn.execute(
            "SELECT status, body, fetched_at FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
        return CacheEntry(r["status"], r["body"], r["fetched_at"]) if r else None

    def cache_put(self, key: str, status: int, body: str, fetched_at: float | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO http_cache (key, status, body, fetched_at) VALUES (?, ?, ?, ?)",
            (key, status, body, time.time() if fetched_at is None else fetched_at),
        )

    def _tx(self) -> "_Tx":
        return _Tx(self.conn)


class _Tx:
    """Explicit transaction for an autocommit connection (isolation_level=None)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> None:
        self.conn.execute("BEGIN")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
