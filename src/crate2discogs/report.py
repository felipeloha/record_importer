from collections import Counter
from dataclasses import dataclass

from crate2discogs.db import STAGES, Repo


@dataclass(frozen=True)
class Status:
    stages: dict[str, int]
    outcomes: dict[str, int]  # "auto", "review", ...; user choices as "<outcome> (user)"
    errors: int


def status_counts(repo: Repo) -> Status:
    stages = Counter(p.stage for p in repo.all_photos())
    outcomes: Counter[str] = Counter()
    for row in repo.conn.execute("SELECT outcome, resolved_by FROM resolutions"):
        outcomes[row["outcome"] + (" (user)" if row["resolved_by"] == "user" else "")] += 1
    errors = repo.conn.execute("SELECT COUNT(*) FROM photos WHERE last_error IS NOT NULL").fetchone()[0]
    return Status({s: stages.get(s, 0) for s in STAGES}, dict(sorted(outcomes.items())), errors)


def format_status(s: Status) -> str:
    lines = ["stage            photos"]
    lines += [f"  {stage:<14} {n:>6}" for stage, n in s.stages.items()]
    lines.append(f"  {'total':<14} {sum(s.stages.values()):>6}")
    lines.append("")
    lines.append("outcome          photos")
    lines += [f"  {o:<14} {n:>6}" for o, n in s.outcomes.items()] or ["  (none yet)"]
    if s.errors:
        lines.append(f"\nphotos with errors: {s.errors} (see `export --csv`)")
    return "\n".join(lines)
