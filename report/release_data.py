"""Load the published raw measurements pinned by the report."""

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zipfile import ZipFile


RELEASES = "https://github.com/KimmoZAG/SuperLoad/releases"


@dataclass(frozen=True)
class ResultsRelease:
    tag: str
    asset: str
    sha256: str

    @classmethod
    def read(cls, manifest):
        return cls(**json.loads(manifest.read_text(encoding="utf-8")))

    @property
    def asset_url(self):
        return f"{RELEASES}/download/{quote(self.tag, safe='')}/{quote(self.asset, safe='')}"

    def matches(self, archive):
        return archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == self.sha256

    @contextmanager
    def unpack(self, cache):
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / f"{self.sha256}.zip"
        with tempfile.TemporaryDirectory(dir=cache) as work:
            temporary = Path(work)
            if not self.matches(archive):
                incoming = temporary / "download.zip"
                request = Request(self.asset_url, headers={"User-Agent": "SuperLoad report"})
                with urlopen(request, timeout=25) as response, incoming.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
                if not self.matches(incoming):
                    raise ValueError(f"SHA-256 mismatch for {self.asset_url}")
                incoming.replace(archive)
            extracted = temporary / "measurements"
            with ZipFile(archive) as source:
                source.extractall(extracted)
            yield extracted
