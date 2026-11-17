"""Keep the original delivery file under its checksum."""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from stiflyt2.config import settings
from stiflyt2.importer import geonorge


@dataclass(frozen=True)
class Kept:
    artifact: Path          # the original file, never overwritten
    sha256: str
    size: int
    url: str
    source_updated_at: object

    @property
    def extracted(self) -> Path:
        return self.artifact.parent / "x"

    def extract(self) -> Path:
        """Unzip next to the artifact and return the directory."""
        if not self.extracted.exists():
            with zipfile.ZipFile(self.artifact) as z:
                z.extractall(self.extracted)
        return self.extracted

    def discard_extracted(self) -> None:
        """The zip is the kept original; the unpacked copy can always be made again."""
        shutil.rmtree(self.extracted, ignore_errors=True)

    def manifest(self, **extra) -> dict:
        return {"file": self.artifact.name, "bytes": self.size, "url": self.url, **extra}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local(file: Path) -> geonorge.Fetched:
    """A delivery file that is already on disk."""
    return geonorge.Fetched(file, sha256(file), file.stat().st_size, file.as_uri(), None)


def keep(source_key: str, fetched: geonorge.Fetched, *, move: bool = False) -> Kept:
    """File the delivery under its checksum. `move` is for our own downloads."""
    d = settings.data_dir / source_key / fetched.sha256[:16]
    d.mkdir(parents=True, exist_ok=True)
    artifact = d / fetched.path.name
    if not artifact.exists():
        (shutil.move if move else shutil.copyfile)(fetched.path, artifact)
    elif move:
        fetched.path.unlink()
    return Kept(artifact, fetched.sha256, fetched.size, fetched.url, fetched.source_updated_at)
