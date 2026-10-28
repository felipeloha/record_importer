import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from crate2discogs.db import STAGES, Repo
from crate2discogs.dedupe import groups
from crate2discogs.pipeline import load_extraction
from crate2discogs.scoring import our_eans

SECONDS_PER_REVIEW = 30  # rough time to decide one item in the review UI

CSV_COLUMNS = [
    "photo_id",
    "path",
    "stage",
    "outcome",
    "resolved_by",
    "release_id",
    "score",
    "photo_type",
    "artist",
    "title",
    "label",
    "catno",
    "ean",
    "disc_count",
    "year",
    "country",
    "error",
    "discogs_url",
]


@dataclass(frozen=True)
class Status:
    stages: dict[str, int]
    outcomes: dict[str, int]  # "auto", "review", "not_found"; user decisions as "picked (user)"/"skip (user)"
    errors: int
    failure_reasons: list[tuple[str, int]] = field(default_factory=list)
    review_items: int = 0  # open decisions + multi-photo groups


def _reason(error: str) -> str:
    """Group similar errors: keep the step and the first words."""
    return " ".join(error.split()[:4])


def status_counts(repo: Repo) -> Status:
    stages = Counter(p.stage for p in repo.all_photos())
    outcomes: Counter[str] = Counter()
    open_items = 0
    for row in repo.conn.execute("SELECT outcome, resolved_by FROM resolutions"):
        if row["resolved_by"] == "user":
            outcomes["skip (user)" if row["outcome"] == "skip" else "picked (user)"] += 1
        else:
            outcomes[row["outcome"]] += 1
            open_items += row["outcome"] in ("review", "not_found")
    errors = [r[0] for r in repo.conn.execute("SELECT last_error FROM photos WHERE last_error IS NOT NULL")]
    reasons = Counter(_reason(e) for e in errors).most_common(5)
    multi = sum(g.needs_copy_question for g in groups(repo))
    return Status(
        {s: stages.get(s, 0) for s in STAGES},
        dict(sorted(outcomes.items())),
        len(errors),
        reasons,
        open_items + multi,
    )


def format_status(s: Status) -> str:
    lines = ["stage            photos"]
    lines += [f"  {stage:<14} {n:>6}" for stage, n in s.stages.items()]
    lines.append(f"  {'total':<14} {sum(s.stages.values()):>6}")
    lines.append("")
    lines.append("outcome          photos")
    lines += [f"  {o:<14} {n:>6}" for o, n in s.outcomes.items()] or ["  (none yet)"]
    if s.review_items:
        minutes = s.review_items * SECONDS_PER_REVIEW / 60
        lines.append(
            f"\nreview workload: {s.review_items} items (~{minutes:.0f} min) → `crate2discogs review`"
        )
    if s.errors:
        lines.append(f"\nphotos with errors: {s.errors} (details: `export --csv`)")
        lines += [f"  {n:>4}× {reason}" for reason, n in s.failure_reasons]
    return "\n".join(lines)


def export_csv(repo: Repo, path: Path) -> int:
    """One row per photo. Returns the number of rows written."""
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for photo in repo.all_photos():
            res = repo.get_resolution(photo.id)
            e = load_extraction(repo, photo.id)
            cands = repo.get_candidates(photo.id)
            release_id = res.release_id if res else None
            score = next((c.score for c in cands if c.release_id == release_id), None)
            eans = our_eans(e, repo.get_barcodes(photo.id))
            writer.writerow(
                {
                    "photo_id": photo.id,
                    "path": str(photo.path),
                    "stage": photo.stage,
                    "outcome": res.outcome if res else "",
                    "resolved_by": res.resolved_by if res else "",
                    "release_id": release_id or "",
                    "score": "" if score is None else score,
                    "photo_type": (e.photo_type if e else None) or "",
                    "artist": (e.artist if e else None) or "",
                    "title": (e.title if e else None) or "",
                    "label": (e.label if e else None) or "",
                    "catno": (e.catno if e else None) or "",
                    "ean": eans[0] if eans else "",
                    "disc_count": (e.disc_count if e else None) or "",
                    "year": (e.year if e else None) or "",
                    "country": (e.country if e else None) or "",
                    "error": photo.last_error or "",
                    "discogs_url": f"https://www.discogs.com/release/{release_id}" if release_id else "",
                }
            )
            rows += 1
    return rows
