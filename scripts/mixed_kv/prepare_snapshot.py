"""Prepare the pinned upstream snapshot for the mixed-load pilot."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .trace import PAGES_PER_PROFILE


def replace_once(text, old, new, path):
    if text.count(old) != 1:
        raise ValueError(f"unexpected source in {path}; use a fresh experiment snapshot")
    return text.replace(old, new)


def prepare_source(src):
    workloads_path = src / "superload/dataset/workloads.py"
    workloads = replace_once(
        workloads_path.read_text(encoding="utf-8"),
        f"PAGE_COUNT = {PAGES_PER_PROFILE}",
        f"PAGE_COUNT = {2 * PAGES_PER_PROFILE}",
        workloads_path,
    )
    roce_path = src / "superload/schemes/roce.py"
    roce = roce_path.read_text(encoding="utf-8")
    roce = replace_once(
        roce,
        "def __init__(self, args, spec, seed, device, max_pages):",
        "def __init__(self, args, spec, seed, device, max_pages, *, external_buffer=False):",
        roce_path,
    )
    roce = replace_once(
        roce,
        "        self.max_pages = max_pages\n",
        "        self.max_pages = max_pages\n        self.external_buffer = external_buffer\n",
        roce_path,
    )
    roce = replace_once(
        roce,
        "            self.buffer = HostBuffer(self.max_pages * self.spec.page_bytes)\n"
        "            opening.callback(self.buffer.close)\n",
        "            if not self.external_buffer:\n"
        "                self.buffer = HostBuffer(self.max_pages * self.spec.page_bytes)\n"
        "                opening.callback(self.buffer.close)\n",
        roce_path,
    )
    roce = replace_once(
        roce,
        "    def load(self, pages, dest):\n",
        "    def receive(self, pages, buffer):\n",
        roce_path,
    )
    roce = replace_once(
        roce,
        """        self.endpoint.transfer(self.buffer.address, amount, send=False)
        upload_start = time.perf_counter_ns()
        self.device.upload(dest, self.buffer.address, amount)
        end = time.perf_counter_ns()
        return {
            "storage_bytes": amount, "read_calls": ready["calls"], "disk_ns": ready["disk_ns"],
            "roce_bytes": amount, "roce_ns": upload_start - transfer_start,
            "hbm_bytes": amount, "h2d_ns": end - upload_start,
            "control_ns": max(0, transfer_start - start - ready["disk_ns"]),
        }
""",
        """        if amount > len(buffer.view):
            raise ValueError("RoCE receive buffer is too small")
        self.endpoint.transfer(buffer.address, amount, send=False)
        received = time.perf_counter_ns()
        return {
            "storage_bytes": amount, "read_calls": ready["calls"], "disk_ns": ready["disk_ns"],
            "roce_bytes": amount, "roce_ns": received - transfer_start,
            "control_ns": max(0, transfer_start - start - ready["disk_ns"]),
        }

    def load(self, pages, dest):
        stats = self.receive(pages, self.buffer)
        amount = stats["storage_bytes"]
        upload_start = time.perf_counter_ns()
        self.device.upload(dest, self.buffer.address, amount)
        return dict(stats, hbm_bytes=amount, h2d_ns=time.perf_counter_ns() - upload_start)
""",
        roce_path,
    )
    workloads_path.write_text(workloads, encoding="utf-8")
    roce_path.write_text(roce, encoding="utf-8")


def make_layer_major(directory, tiny):
    from superload.dataset.corpus import page_bytes, page_extents, write_at
    from superload.dataset.workloads import Geometry, PAGE_COUNT

    if PAGE_COUNT != 2 * PAGES_PER_PROFILE:
        raise ValueError("prepare the experiment snapshot before generating its corpus")
    directory.mkdir(parents=True)
    spec = Geometry(layers=2, heads=1, head_dim=8, tokens=4) if tiny else Geometry()
    seed = 1729
    with (directory / "layer-major.bin").open("xb") as handle:
        for p in range(PAGE_COUNT):
            payload = page_bytes(seed, p, spec.page_bytes)
            for extent in page_extents("layer-major", spec, p):
                write_at(
                    handle.fileno(),
                    payload[extent.target : extent.target + extent.size],
                    extent.offset,
                )
    manifest = {"schema": 1, "seed": seed, "geometry": asdict(spec)}
    (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_subparsers(dest="operation", required=True)
    patch_parser = mode.add_parser("patch")
    patch_parser.add_argument("source", type=Path)
    corpus = mode.add_parser("corpus")
    corpus.add_argument("destination", type=Path)
    corpus.add_argument("--tiny", action="store_true")
    args = parser.parse_args()
    if args.operation == "patch":
        prepare_source(args.source)
    else:
        make_layer_major(args.destination, args.tiny)


if __name__ == "__main__":
    main()
