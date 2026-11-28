"""Verify the measurement protocol and recompute the public metrics from raw records."""

import hashlib
import json
import random
import re
from itertools import product

from report.comparison_data import GAPS_MS, METHODS, PROFILES, read, summarize


SOURCE_FILES = {
    "superload/benchmark/comparison.py",
    "superload/cli.py",
    "superload/hardware/device.py",
    "superload/schemes/superload.py",
}
ROLES = ("small", "large")
RUN_ARTIFACTS = ("plan.json", "implementation.sha256", "verification.json", "audit.json", "environment.json",
                 *(f"{profile}.json" for profile in PROFILES))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_sources(receipt, source):
    hashes = {}
    for line in receipt.read_text(encoding="ascii").splitlines():
        digest, path = line.split()
        if path not in SOURCE_FILES or path in hashes or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError(f"invalid implementation checksum entry for {path}")
        if sha256(source / path) != digest:
            raise ValueError(f"implementation checksum mismatch for {path}")
        hashes[path] = digest
    if set(hashes) != SOURCE_FILES:
        raise ValueError("incomplete implementation checksum receipt")
    return hashes


def pair_size(pair):
    return sum(len(pair[role]) for role in ROLES)


def extreme_pairs(plan, selected):
    ordered = sorted(selected, key=lambda index: pair_size(plan["pairs"][index]))
    return list(dict.fromkeys((ordered[0], ordered[-1])))


def check_verification(receipt, plan, plan_digest, selected):
    if receipt["plan_sha256"] != plan_digest:
        raise ValueError("HBM verification plan checksum mismatch")
    expected = set(product(PROFILES, extreme_pairs(plan, selected), GAPS_MS))
    seen = set()
    total = 0
    for window in receipt["windows"]:
        key = window["profile"], window["pair"], window["gap_ms"]
        if key not in expected or key in seen:
            raise ValueError(f"unexpected or duplicate HBM verification window {key}")
        seen.add(key)
        pages = pair_size(plan["pairs"][window["pair"]])
        if (window["hbm_cleared"] is not True or window["pages"] != pages
                or window["bytes"] != pages * plan["page_bytes"]):
            raise ValueError(f"incomplete HBM verification at {key}")
        total += pages
    if seen != expected:
        raise ValueError("missing HBM verification windows")
    return {"windows": len(seen), "pages": total}


def check_order(run, label, pair_count):
    """Replay the recorded randomization, including the RNG state across trials."""
    rng = random.Random(run["run_seed"])
    if len(run["pairs"]) < pair_count:
        selected = sorted(rng.sample(range(pair_count), len(run["pairs"])))
        if selected != run["pairs"]:
            raise ValueError(f"pair selection does not match the recorded seed in {label}")
    for index, trial in enumerate(run["trials"], start=1):
        pairs = run["pairs"].copy()
        rng.shuffle(pairs)
        expected = []
        for pair in pairs:
            conditions = list(product(GAPS_MS, METHODS, run["host_buffers"]))
            rng.shuffle(conditions)
            expected.extend((pair, *condition) for condition in conditions)
        actual = [(row["pair"], row["gap_ms"], row["method"], row["host_buffer"]) for row in trial]
        if actual != expected:
            raise ValueError(f"randomized execution order mismatch in {label} trial {index}")


def audit_results(directory, source):
    manifest = directory / "run-manifest.json"
    if manifest.is_file():
        digests = json.loads(manifest.read_bytes())["sha256"]
        if set(digests) != set(RUN_ARTIFACTS):
            raise ValueError("incomplete run manifest")
        for filename in RUN_ARTIFACTS:
            if sha256(directory / filename) != digests[filename]:
                raise ValueError(f"run manifest checksum mismatch for {filename}")
    plan, runs = read(directory)
    plan_digest = sha256(directory / "plan.json")
    files = [mode for mode in PROFILES if (directory / f"{mode}.json").is_file()]
    for mode in files:
        with (directory / f"{mode}.json").open(encoding="utf-8") as stream:
            check_order(json.load(stream), mode, len(plan["pairs"]))
    checksums = check_sources(directory / "implementation.sha256", source)
    with (directory / "verification.json").open(encoding="utf-8") as stream:
        verification = check_verification(json.load(stream), plan, plan_digest, runs["direct"]["pairs"])
    summary = summarize(plan, runs)
    scores = []
    for profile, measured in summary["measurements"].items():
        for gap, methods in measured["gaps"].items():
            for metric, best in (("small_p95", min), ("large_p95", min), ("gib_s", max)):
                control, value = best(
                    ((name, row[metric]["median"]) for name, row in methods.items() if name != "superload"),
                    key=lambda item: item[1],
                )
                candidate = methods["superload"][metric]["median"]
                scores.append({"profile": profile, "gap_ms": gap, "metric": metric, "control": control,
                               "control_value": value, "superload": candidate,
                               "wins": candidate != value and best(candidate, value) == candidate})
    return {
        "plan_sha256": plan_digest,
        "implementation_sha256": checksums,
        "timed_windows": sum(len(trial) for run in runs.values() for trial in run["trials"]),
        "hbm_verification": verification,
        "wins": sum(score["wins"] for score in scores),
        "comparisons": scores,
        "summary": summary,
    }
