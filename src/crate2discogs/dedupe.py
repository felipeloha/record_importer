from dataclasses import dataclass

from crate2discogs.db import Repo


@dataclass(frozen=True)
class Group:
    """All photos resolved to one release. `copies` = total copies wanted in the collection."""

    release_id: int
    photo_ids: tuple[int, ...]
    copies: int

    @property
    def needs_copy_question(self) -> bool:
        """Several photos: same record photographed twice, or several copies? Asked in review."""
        return len(self.photo_ids) > 1


def groups(repo: Repo) -> list[Group]:
    """Photos count when their resolution has a release_id and is not `skip`.

    Auto resolutions carry a release_id only when the outcome is `auto`; user picks always carry one.
    """
    rows = repo.conn.execute(
        "SELECT photo_id, release_id FROM resolutions"
        " WHERE release_id IS NOT NULL AND outcome != 'skip' ORDER BY release_id, photo_id"
    )
    by_release: dict[int, list[int]] = {}
    for r in rows:
        by_release.setdefault(r["release_id"], []).append(r["photo_id"])
    return [Group(rid, tuple(pids), repo.get_copies(rid)) for rid, pids in by_release.items()]
