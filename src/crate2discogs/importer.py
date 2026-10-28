import hashlib
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from crate2discogs.db import Repo

pillow_heif.register_heif_opener()

EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})


@dataclass(frozen=True)
class ImportResult:
    new: int
    existing: int


def scan(directory: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        p
        for p in directory.glob(pattern)
        if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(".")
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_oriented(path: Path) -> Image.Image:
    """Open any supported image with EXIF orientation applied, as RGB."""
    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def make_work_copy(src: Path, dst: Path, max_edge: int) -> None:
    img = open_oriented(src)
    img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    img.save(tmp, format="JPEG", quality=90)
    tmp.replace(dst)


def import_file(repo: Repo, path: Path, work_dir: Path, max_edge: int) -> tuple[int, bool]:
    """Import one photo. Returns (photo_id, created)."""
    sha = sha256_file(path)
    if repo.has_sha(sha):
        return repo.upsert_photo(sha, path.resolve(), work_dir / f"{sha}.jpg")
    work_path = (work_dir / f"{sha}.jpg").resolve()
    make_work_copy(path, work_path, max_edge)
    return repo.upsert_photo(sha, path.resolve(), work_path)


def import_dir(
    repo: Repo, directory: Path, work_dir: Path, max_edge: int, recursive: bool = False
) -> ImportResult:
    new = existing = 0
    for path in scan(directory, recursive):
        _, created = import_file(repo, path, work_dir, max_edge)
        if created:
            new += 1
        else:
            existing += 1
    return ImportResult(new, existing)
