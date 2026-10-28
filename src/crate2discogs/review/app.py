import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from crate2discogs.db import Photo, Repo, ResolutionRow
from crate2discogs.dedupe import Group, groups
from crate2discogs.discogs import DiscogsError, DiscogsNotFound
from crate2discogs.models import Extraction
from crate2discogs.pipeline import load_extraction, resolve_photo
from crate2discogs.scoring import candidate_artist_title, candidate_catnos, candidate_labels

HOST = "127.0.0.1"
PORT = 8765
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

_RELEASE_URL = re.compile(r"discogs\.com/(?:[a-z]{2}/)?release/(\d+)", re.IGNORECASE)


class ReleaseLookup(Protocol):
    def release(self, release_id: int) -> dict[str, Any]: ...


class InvalidReleaseInput(ValueError):
    pass


def parse_release_input(value: str) -> int:
    """Accept a release ID ("123", "r123") or a Discogs release URL. Master URLs are rejected."""
    v = value.strip()
    if re.search(r"/master/", v, re.IGNORECASE):
        raise InvalidReleaseInput("that is a master URL; open the exact pressing and paste its /release/ URL")
    if m := _RELEASE_URL.search(v):
        return int(m.group(1))
    if m := re.fullmatch(r"\[?r?(\d+)\]?", v, re.IGNORECASE):
        return int(m.group(1))
    raise InvalidReleaseInput(f"not a Discogs release ID or URL: {value!r}")


@dataclass
class CandidateView:
    release_id: int
    score: float
    matched: list[str]
    artist: str
    title: str
    label: str
    catno: str
    year: str
    country: str
    format: str
    thumb: str | None
    url: str


def candidate_view(
    payload: dict[str, Any], score: float = 0.0, matched: list[str] | None = None
) -> CandidateView:
    artist, title = candidate_artist_title(payload)
    if isinstance(payload.get("format"), list):
        fmt = ", ".join(payload["format"])
    else:
        fmt = ", ".join(
            f"{f.get('qty', '1')}×{f.get('name', '')} {' '.join(f.get('descriptions', []))}".strip()
            for f in payload.get("formats", [])
        )
    thumb = payload.get("thumb") or payload.get("cover_image")
    if not thumb and payload.get("images"):
        thumb = payload["images"][0].get("uri150")
    rid = int(payload["id"])
    return CandidateView(
        release_id=rid,
        score=score,
        matched=matched or [],
        artist=artist,
        title=title,
        label=", ".join(dict.fromkeys(candidate_labels(payload))),
        catno=", ".join(dict.fromkeys(candidate_catnos(payload))),
        year=str(payload.get("year") or payload.get("released") or ""),
        country=str(payload.get("country") or ""),
        format=fmt,
        thumb=thumb or None,
        url=f"https://www.discogs.com/release/{rid}",
    )


@dataclass
class Item:
    photo: Photo
    resolution: ResolutionRow | None
    extraction: Extraction | None
    group: Group | None = None
    candidates: list[CandidateView] = field(default_factory=list)

    @property
    def summary(self) -> str:
        e = self.extraction
        if e is None:
            return "(nothing extracted)"
        parts = [" – ".join(x for x in (e.artist, e.title) if x), e.label or "", e.catno or ""]
        return " · ".join(p for p in parts if p) or "(nothing legible)"

    @property
    def decided_by_user(self) -> bool:
        return self.resolution is not None and self.resolution.resolved_by == "user"

    @property
    def editable(self) -> bool:
        return self.photo.stage == "resolved"


@dataclass
class Queue:
    needs_review: list[Item]  # review / not_found, not yet decided by the user
    groups: list[tuple[Group, list[Item]]]  # several photos → one release: copies question
    auto: list[Item]  # single-photo auto results (collapsed)
    decided: list[Item]  # decided by the user (collapsed)

    def pending_ids(self) -> list[int]:
        return [i.photo.id for i in self.needs_review]


def _item(repo: Repo, photo: Photo, group_by_photo: dict[int, Group], with_candidates: bool = False) -> Item:
    item = Item(
        photo, repo.get_resolution(photo.id), load_extraction(repo, photo.id), group_by_photo.get(photo.id)
    )
    if with_candidates:
        for c in repo.get_candidates(photo.id)[:3]:
            item.candidates.append(candidate_view(json.loads(c.payload_json), c.score))
    return item


def build_queue(repo: Repo) -> Queue:
    all_groups = groups(repo)
    by_photo = {pid: g for g in all_groups for pid in g.photo_ids}
    q = Queue([], [], [], [])
    photos = {p.id: p for p in repo.all_photos() if p.stage in ("resolved", "added")}
    for photo in photos.values():
        item = _item(repo, photo, by_photo)
        res = item.resolution
        if res is None:
            continue
        if res.resolved_by == "user":
            q.decided.append(item)
        elif res.outcome in ("review", "not_found"):
            q.needs_review.append(item)
        elif not (item.group and item.group.needs_copy_question):
            q.auto.append(item)
    for g in all_groups:
        if g.needs_copy_question:
            q.groups.append((g, [_item(repo, photos[pid], by_photo) for pid in g.photo_ids if pid in photos]))
    return q


def create_app(repo: Repo, discogs: ReleaseLookup, margin: float) -> FastAPI:
    app = FastAPI(title="crate2discogs review", docs_url=None, redoc_url=None)

    # One request at a time: `repo` (and the Discogs cache) share a single sqlite connection, and the
    # browser loads all queue thumbnails in parallel. Single local user, so serialising costs nothing.
    lock = asyncio.Lock()

    @app.middleware("http")
    async def one_at_a_time(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        async with lock:
            return await call_next(request)

    def photo_or_404(photo_id: int) -> Photo:
        photo = repo.get_photo(photo_id)
        if photo is None:
            raise HTTPException(404, "unknown photo")
        return photo

    def editable_or_409(photo: Photo) -> None:
        if photo.stage != "resolved":
            raise HTTPException(
                409, f"photo is at stage {photo.stage!r}; only resolved photos can be changed"
            )

    def next_url(after: int) -> str:
        pending = build_queue(repo).pending_ids()
        later = [pid for pid in pending if pid > after] or pending
        return f"/photos/{later[0]}" if later else "/"

    def render_photo(
        request: Request, photo: Photo, error: str | None = None, status: int = 200
    ) -> HTMLResponse:
        by_photo = {pid: g for g in groups(repo) for pid in g.photo_ids}
        item = _item(repo, photo, by_photo, with_candidates=True)
        chosen = None
        res = item.resolution
        if res and res.release_id is not None:
            chosen = next((c for c in item.candidates if c.release_id == res.release_id), None)
            if chosen is None:
                try:
                    chosen = candidate_view(discogs.release(res.release_id))
                except DiscogsError:
                    chosen = None
        barcodes = repo.get_barcodes(photo.id)
        return TEMPLATES.TemplateResponse(
            request,
            "photo.html",
            {"item": item, "chosen": chosen, "barcodes": barcodes, "error": error},
            status_code=status,
        )

    @app.get("/", response_class=HTMLResponse)
    def queue_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "queue.html", {"q": build_queue(repo)})

    @app.get("/photos/{photo_id}/image")
    def photo_image(photo_id: int) -> FileResponse:
        photo = photo_or_404(photo_id)
        if not photo.work_path.is_file():
            raise HTTPException(404, "work copy missing")
        return FileResponse(photo.work_path, media_type="image/jpeg")

    @app.get("/photos/{photo_id}", response_class=HTMLResponse)
    def photo_page(request: Request, photo_id: int) -> HTMLResponse:
        return render_photo(request, photo_or_404(photo_id))

    def _save_user(photo: Photo, outcome: str | None, release_id: int | None) -> None:
        prev = repo.get_resolution(photo.id)
        kept = prev.outcome if prev and prev.outcome != "skip" else "review"
        repo.save_resolution(photo.id, ResolutionRow(outcome or kept, release_id, "user"))  # type: ignore[arg-type]

    @app.post("/photos/{photo_id}/pick")
    def pick(photo_id: int, release_id: Annotated[int, Form()]) -> Response:
        photo = photo_or_404(photo_id)
        editable_or_409(photo)
        _save_user(photo, None, release_id)
        return RedirectResponse(next_url(photo_id), 303)

    @app.post("/photos/{photo_id}/paste")
    def paste(request: Request, photo_id: int, value: Annotated[str, Form()]) -> Response:
        photo = photo_or_404(photo_id)
        editable_or_409(photo)
        try:
            release_id = parse_release_input(value)
            discogs.release(release_id)
        except InvalidReleaseInput as e:
            return render_photo(request, photo, str(e), 400)
        except DiscogsNotFound:
            return render_photo(request, photo, f"release {value.strip()!r} does not exist on Discogs", 400)
        except DiscogsError as e:
            return render_photo(request, photo, f"could not check the release: {e}", 502)
        _save_user(photo, None, release_id)
        return RedirectResponse(next_url(photo_id), 303)

    @app.post("/photos/{photo_id}/skip")
    def skip(photo_id: int) -> Response:
        photo = photo_or_404(photo_id)
        editable_or_409(photo)
        _save_user(photo, "skip", None)
        return RedirectResponse(next_url(photo_id), 303)

    @app.post("/photos/{photo_id}/undo")
    def undo(photo_id: int) -> Response:
        """Drop the user's decision and restore the automatic result (no Discogs calls)."""
        photo = photo_or_404(photo_id)
        editable_or_409(photo)
        repo.delete_resolution(photo.id)
        resolve_photo(repo, photo, margin)
        return RedirectResponse(f"/photos/{photo_id}", 303)

    @app.post("/releases/{release_id}/copies")
    def set_copies(
        release_id: int, copies: Annotated[int, Form(ge=1, le=99)], back: Annotated[str, Form()] = "/"
    ) -> Response:
        repo.set_copies(release_id, copies)
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", 303)

    return app
