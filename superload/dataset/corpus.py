"""KV corpus layouts and an independent byte-level verification oracle."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .geometry import Geometry


LAYOUTS = ("layer-major", "page-first")


@dataclass(frozen=True)
class Extent:
    offset: int
    target: int
    size: int


def page_bytes(seed, page, size):
    key = f"superload-v1/kv/{seed}/{page}".encode()
    return hashlib.shake_256(key).digest(size)


def page_extents(layout, spec, page, target, page_count):
    plane = spec.plane_bytes
    if layout == "page-first":
        return [Extent(page * spec.page_bytes, target, spec.page_bytes)]
    if layout == "layer-major":
        return [
            Extent((i * page_count + page) * plane, target + i * plane, plane)
            for i in range(spec.layers * 2)
        ]
    raise ValueError(f"unknown layout: {layout}")


def make_plan(layout, spec, pages, page_count):
    spans = []
    for i, page in enumerate(pages):
        for current in page_extents(layout, spec, page, i * spec.page_bytes, page_count):
            if spans:
                prev = spans[-1]
                if prev.offset + prev.size == current.offset and prev.target + prev.size == current.target:
                    spans[-1] = Extent(prev.offset, prev.target, prev.size + current.size)
                    continue
            spans.append(current)
    return tuple(spans)


def write_at(fd, data, offset):
    view = memoryview(data)
    while view:
        count = os.pwrite(fd, view, offset)
        if count <= 0:
            raise OSError("pwrite made no progress")
        offset += count
        view = view[count:]


def generate(directory, spec, seed, *, page_count, layouts=LAYOUTS):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if manifest.exists() or any((directory / f"{name}.bin").exists() for name in layouts):
        raise FileExistsError(f"choose an empty corpus directory: {directory}")
    files = {}
    try:
        for name in layouts:
            files[name] = (directory / f"{name}.bin").open("xb")
        for page in range(page_count):
            canonical = page_bytes(seed, page, spec.page_bytes)
            for name, handle in files.items():
                for extent in page_extents(name, spec, page, 0, page_count):
                    write_at(handle.fileno(), canonical[extent.target:extent.target + extent.size], extent.offset)
        for handle in files.values():
            os.fsync(handle.fileno())
    except BaseException:
        for name, handle in files.items():
            handle.close()
            (directory / f"{name}.bin").unlink()
        raise
    else:
        for handle in files.values():
            handle.close()
    manifest.write_text(json.dumps({"schema": 1, "seed": seed, "geometry": asdict(spec)}) + "\n", encoding="utf-8")


def read_manifest(directory):
    manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != 1:
        raise ValueError("unsupported corpus format")
    return Geometry(**manifest["geometry"]), int(manifest["seed"])


def open_corpus(directory, layout, spec, mode, *, page_count):
    path = Path(directory) / f"{layout}.bin"
    flags = os.O_RDONLY
    if mode == "direct":
        if not hasattr(os, "O_DIRECT") or spec.plane_bytes % os.sysconf("SC_PAGESIZE"):
            raise ValueError("direct I/O requires Linux and page-aligned KV planes")
        flags |= os.O_DIRECT
    fd = os.open(path, flags)
    if os.fstat(fd).st_size != page_count * spec.page_bytes:
        os.close(fd)
        raise ValueError(f"invalid corpus size: {path.name}")
    return fd


def read_spans(fd, storage, spans):
    calls = byte_count = 0
    for extent in spans:
        pos = 0
        while pos < extent.size:
            target = extent.target + pos
            count = os.preadv(fd, [storage[target:extent.target + extent.size]], extent.offset + pos)
            if not count:
                raise EOFError(f"short corpus read at offset {extent.offset + pos}")
            calls += 1
            byte_count += count
            pos += count
    return calls, byte_count
