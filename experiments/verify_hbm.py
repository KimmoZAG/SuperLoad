"""Clear HBM and verify all KV bytes in the smallest and largest measured pairs."""

import ctypes
import os
from contextlib import ExitStack

from report.comparison_data import GAPS_MS, PROFILES, read
from superload.benchmark.prefix import CORPUS_SEED
from superload.dataset.corpus import open_corpus, page_bytes
from superload.dataset.prefix import GEOMETRY
from superload.hardware.device import Ascend, stage_buffer
from superload.schemes.superload import ReloadPipeline, pipeline_config

from .audit import ROLES, extreme_pairs, pair_size, sha256


def verify_pair(device, loader, zero, base, pair, gap):
    page = GEOMETRY.page_bytes
    size = pair_size(pair)
    for offset in range(0, size * page, page):
        device.upload(base + offset, zero.address, page)
    _, jobs = loader.run(pair, gap, base)
    if len(jobs) != len(ROLES) or {job.role for job in jobs} != set(ROLES):
        raise RuntimeError("unexpected requests in the HBM completion record")
    completed = {job.role: job for job in jobs}
    expected_address = base
    for role in ROLES:
        job = completed[role]
        blocks = tuple(sorted(pair[role]))
        if job.address != expected_address or job.blocks != blocks:
            raise RuntimeError(f"unexpected HBM placement for {role} request")
        for index, block in enumerate(blocks):
            found = device.download(expected_address + index * page, page)
            if found != page_bytes(CORPUS_SEED, block, page):
                raise RuntimeError(f"HBM data mismatch for {role} page {block} at gap {gap} ms")
        expected_address += len(blocks) * page
    return size


def verify_hbm(directory, corpus, device_id, numa_node):
    plan, runs = read(directory)
    pairs = plan["pairs"]
    selected = extreme_pairs(plan, runs["direct"]["pairs"])
    count = len(plan["file_page_hashes"])
    page = GEOMETRY.page_bytes
    length = max(pair_size(pairs[index]) for index in selected) * page
    results = []
    with Ascend(device_id) as device, device.allocate(length) as base, ExitStack() as common:
        zero = stage_buffer(common, device, page)
        ctypes.memset(zero.address, 0, page)
        for profile, (mode, host) in PROFILES.items():
            node = numa_node if profile == "buffered" else None
            with ExitStack() as resources:
                fd = open_corpus(corpus, "page-first", GEOMETRY, mode, page_count=count)
                resources.callback(os.close, fd)
                loader = ReloadPipeline(resources, device, fd, count, pipeline_config(mode, host), node)
                for index in selected:
                    for gap in GAPS_MS:
                        checked = verify_pair(device, loader, zero, base, pairs[index], gap)
                        results.append({"profile": profile, "pair": index, "gap_ms": gap, "pages": checked,
                                        "bytes": checked * page, "hbm_cleared": True})
                        print(f"{profile} pair {index}, gap {gap} ms: {checked} HBM pages verified", flush=True)
    return {"plan_sha256": sha256(directory / "plan.json"), "windows": results}
