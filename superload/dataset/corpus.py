"""KV corpus layouts and an independent byte-level verification oracle."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .workloads import Geometry, PAGE_COUNT


LAYOUTS = ("layer-major", "page-first", "interleaved")


@dataclass(frozen=True)
class Extent:
    offset: int
    target: int
    size: int


def page_bytes(seed, page, size):
    key = f"superload-v1/kv/{seed}/{page}".encode()
    return hashlib.shake_256(key).digest(size)


def interleave(spec, page):
    """Turn each plane pair from [K tokens][V tokens] into [K0,V0,K1,V1,...]."""
    plane = spec.plane_bytes
    vector = spec.vector_bytes
    result = bytearray(len(page))
    for layer in range(spec.layers):
        k = 2 * layer * plane
        v = k + plane
        for token in range(spec.tokens):
            src = token * vector
            dst = k + 2 * src
            result[dst:dst + vector] = page[k + src:k + src + vector]
            result[dst + vector:dst + 2 * vector] = page[v + src:v + src + vector]
    return result


def page_extents(layout, spec, page, target=0, page_count=PAGE_COUNT):
    plane = spec.plane_bytes
    if layout == "page-first":
        return [Extent(page * spec.page_bytes, target, spec.page_bytes)]
    if layout == "layer-major":
        return [
            Extent((i * page_count + page) * plane, target + i * plane, plane)
            for i in range(spec.layers * 2)
        ]
    if layout == "interleaved":
        span = 2 * plane
        return [
            Extent((i * page_count + page) * span, target + i * span, span)
            for i in range(spec.layers)
        ]
    raise ValueError(f"unknown layout: {layout}")


def make_plan(layout, spec, pages, page_count=PAGE_COUNT):
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


def generate(directory, spec, seed, *, page_count=PAGE_COUNT, layouts=LAYOUTS):
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
                payload = interleave(spec, canonical) if name == "interleaved" else canonical
                for extent in page_extents(name, spec, page, page_count=page_count):
                    write_at(handle.fileno(), payload[extent.target:extent.target + extent.size], extent.offset)
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


def open_corpus(directory, layout, spec, mode, *, page_count=PAGE_COUNT):
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
