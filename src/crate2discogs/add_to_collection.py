import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from crate2discogs.db import Repo
from crate2discogs.dedupe import Group, groups
from crate2discogs.discogs import DiscogsError

log = logging.getLogger(__name__)


class Collection(Protocol):
    def collection_instances(self, release_id: int) -> list[dict[str, Any]]: ...
    def add_release(self, folder_id: int, release_id: int) -> int: ...


@dataclass
class PlanRow:
    release_id: int
    photo_ids: tuple[int, ...]
    wanted: int
    already: int | None = None  # instances in the collection (live lookup); None if the lookup failed
    error: str | None = None

    @property
    def to_add(self) -> int:
        return max(0, self.wanted - (self.already or 0)) if self.already is not None else 0


@dataclass
class Summary:
    added: int = 0  # instances added
    skipped: int = 0  # releases already complete
    failed: int = 0  # releases with an error
    rows: list[PlanRow] = field(default_factory=list)


def pending_groups(repo: Repo) -> list[Group]:
    """Groups with at least one photo still at `resolved`. Fully added groups are done."""
    stages = {p.id: p.stage for p in repo.all_photos()}
    return [g for g in groups(repo) if any(stages.get(pid) == "resolved" for pid in g.photo_ids)]


def build_plan(repo: Repo, collection: Collection, tick: Callable[[], None] = lambda: None) -> list[PlanRow]:
    """One row per release: wanted copies, already in the collection (live, never cached), to add."""
    rows: list[PlanRow] = []
    for g in pending_groups(repo):
        row = PlanRow(g.release_id, g.photo_ids, g.copies)
        try:
            row.already = len(collection.collection_instances(g.release_id))
        except DiscogsError as e:
            row.error = str(e)
        rows.append(row)
        tick()
    return rows


def apply_plan(
    repo: Repo,
    collection: Collection,
    folder_id: int,
    rows: list[PlanRow],
    tick: Callable[[], None] = lambda: None,
) -> Summary:
    """The only code path that writes to Discogs.

    Uses the live counts from `build_plan`; after a crash, a re-run builds a fresh plan, so nothing is
    added twice. Rows whose lookup failed are reported as failed and left for the next run.
    """
    summary = Summary(rows=rows)
    for row in rows:
        if row.already is None:
            summary.failed += 1
            tick()
            continue
        to_add = row.to_add
        try:
            for _ in range(to_add):
                instance_id = collection.add_release(folder_id, row.release_id)
                repo.record_collection_add(row.release_id, instance_id, folder_id)
                summary.added += 1
                row.already += 1
                log.info("add release=%s instance=%s folder=%s", row.release_id, instance_id, folder_id)
        except DiscogsError as e:
            row.error = str(e)
            summary.failed += 1
            for pid in row.photo_ids:
                repo.set_error(pid, f"add: {e}")
            log.warning("add release=%s error=%s", row.release_id, e)
            tick()
            continue
        if to_add == 0:
            summary.skipped += 1
        for pid in row.photo_ids:
            photo = repo.get_photo(pid)
            if photo is not None and photo.stage == "resolved":
                repo.set_stage(pid, "added")
                repo.set_error(pid, None)
        tick()
    return summary


def format_plan(rows: list[PlanRow]) -> str:
    lines = [f"{'release':>10}  {'photos':>6}  {'wanted':>6}  {'already':>7}  {'to add':>6}"]
    for r in rows:
        already = "?" if r.already is None else str(r.already)
        line = f"{r.release_id:>10}  {len(r.photo_ids):>6}  {r.wanted:>6}  {already:>7}  {r.to_add:>6}"
        lines.append(line + (f"  error: {r.error}" if r.error else ""))
    total = sum(r.to_add for r in rows)
    lines.append(f"\n{len(rows)} releases, {total} instances to add")
    return "\n".join(lines)
