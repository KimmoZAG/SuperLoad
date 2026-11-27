"""Validate request records and produce paired summaries of the mechanism runs."""
import argparse
import csv
import json
import math
import tarfile
from collections import defaultdict
from pathlib import Path
from statistics import median

from .paper_comparison import JOINT_COMPARISON
from .summarize import quantile


PREFIX = "interactive-prefix"
SPARSE = "sparse-decode"


def records(archive):
    with tarfile.open(archive, "r:gz") as tar:
        for item in tar:
            path = Path(item.name)
            if (
                item.isfile()
                and path.parent.name in ("buffered", "direct", "hbm-smoke")
                and path.suffix == ".json"
            ):
                with tar.extractfile(item) as content:
                    yield path.parent.name, json.load(content)


def validate(mode, row):
    stage = row["submissions"]
    requests = row["requests"]
    totals = row["counts"]
    page = row["page_bytes"]
    assert row["backend"] == "ascend"
    assert all(
        r["arrival_ns"] <= r["first_ns"] <= r["ready_ns"] <= totals["wall_ns"] for r in requests
    )
    assert totals["logical_bytes"] == sum(len(r["pages"]) for r in requests) * page
    assert totals["hbm_bytes"] == totals["logical_bytes"] - totals["shared_transfers_saved"] * page
    if row["scheme"] == "joint":
        assert totals["storage_bytes"] == totals["hbm_bytes"]
        intervals = row["stage_intervals"]
        for phase, key in (("read", "disk_ns"), ("h2d", "h2d_ns")):
            times = [(a, b) for _, name, a, b in intervals if name == phase]
            assert all(a <= b for a, b in times)
            assert all(a[1] <= b[0] for a, b in zip(times, times[1:]))
            assert sum(b - a for a, b in times) == totals[key]
        assert sum(phase == "h2d" for _, phase, _, _ in intervals) == totals["copies"]
    else:
        assert sorted(i for s in stage for i in s["request_ids"]) == sorted(
            r["id"] for r in requests
        )
        assert sum(s["hbm_bytes"] for s in stage) == totals["hbm_bytes"]
        assert (
            sum(s["storage_bytes"] for s in stage) + totals.get("prefetch_bytes", 0)
            == totals["storage_bytes"]
        )
        assert (
            sum(s["read_calls"] for s in stage) + totals.get("prefetch_calls", 0)
            == totals["read_calls"]
        )
    assert totals["verified_pages"] > 0
    assert all(len(s["pages"]) * page <= row["staging_bytes"] for s in stage)
    if mode == "hbm-smoke":
        assert totals["verified_pages"] * page == totals["logical_bytes"]
    for kind, metrics in row["classes"].items():
        sample = [r for r in requests if r["profile"] == kind]
        assert metrics["requests"] == len(sample)
        p95 = quantile([(r["ready_ns"] - r["arrival_ns"]) / 1e6 for r in sample], 0.95)
        assert math.isclose(p95, metrics["p95_ms"], rel_tol=1e-12)
        assert metrics["on_time"] == sum(r["ready_ns"] <= r["deadline_ns"] for r in sample) / len(
            sample
        )


def summarize(key, rows):
    result = dict(zip(("io", "scenario", "scheme"), key))
    result["trials"] = len(rows)
    for name, kind in (("prefix", PREFIX), ("sparse", SPARSE)):
        sample = [r["classes"][kind] for r in rows if kind in r["classes"]]
        if sample:
            values = [a["p95_ms"] for a in sample]
            result[f"{name}_p95_ms"], result[f"{name}_p95_min_ms"], result[f"{name}_p95_max_ms"] = (
                median(values),
                min(values),
                max(values),
            )
            result[f"{name}_on_time"] = median(a["on_time"] for a in sample)
            result[f"{name}_queue_p95_ms"] = median(a["queue_p95_ms"] for a in sample)
        else:
            for field in ("p95_ms", "p95_min_ms", "p95_max_ms", "on_time", "queue_p95_ms"):
                result[f"{name}_{field}"] = None
    result["logical_gib_s"] = median(r["ready_gib_s"] for r in rows)
    for key in ("storage_bytes", "hbm_bytes"):
        result[key + "_per_logical_byte"] = median(
            r["counts"][key] / r["counts"]["logical_bytes"] for r in rows
        )
    for key in (
        "read_calls",
        "shared_transfers_saved",
        "host_hit_pages",
        "prefetch_hit_pages",
        "prefetch_bytes",
        "prefetch_wait_ns",
    ):
        result[key] = median(r["counts"].get(key, 0) for r in rows)
    for key in ("disk_ns", "h2d_ns"):
        result[key[:-3] + "_ms_per_submission"] = (
            None
            if rows[0]["scheme"] == "joint"
            else median(r["counts"][key] / len(r["submissions"]) / 1e6 for r in rows)
        )
    return result


def comparisons(grouped):
    for (mode, scenario, scheme), cases in sorted(grouped.items()):
        controls = ["native"]
        if scheme == "joint":
            controls = JOINT_COMPARISON[:-1]
        elif scheme in ("strata", "bidaw", "solid"):
            controls.append(
                {"strata": "strata-io", "bidaw": "native-lru", "solid": "solid-io"}[scheme]
            )
        for reference in controls:
            if scheme == reference:
                continue
            earlier = grouped.get((mode, scenario, reference), {})
            for seed in sorted(cases.keys() & earlier.keys()):
                old, new = earlier[seed], cases[seed]
                for kind in old["classes"]:
                    a, b = (c["classes"][kind]["p95_ms"] for c in (old, new))
                    yield dict(
                        io=mode,
                        scenario=scenario,
                        profile=kind,
                        seed=seed,
                        scheme=scheme,
                        control=reference,
                        p95_ms=b,
                        control_p95_ms=a,
                        change_pct=(b / a - 1) * 100,
                    )
        if scenario == "mix-balanced":
            for kind, isolation in ((PREFIX, "prefix-only"), (SPARSE, "sparse-only")):
                earlier = grouped.get((mode, isolation, scheme), {})
                for seed in sorted(cases.keys() & earlier.keys()):
                    a, b = (c["classes"][kind]["p95_ms"] for c in (earlier[seed], cases[seed]))
                    yield dict(
                        io=mode,
                        scenario=scenario,
                        profile=kind,
                        seed=seed,
                        scheme=scheme,
                        control=isolation,
                        p95_ms=b,
                        control_p95_ms=a,
                        change_pct=(b / a - 1) * 100,
                    )


def save(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--suite", choices=("mechanisms", "joint"), default="mechanisms")
    args = ap.parse_args()
    groups = defaultdict(dict)
    traces = {}
    counts = defaultdict(int)
    for mode, row in records(args.archive):
        validate(mode, row)
        counts[mode] += 1
        if mode == "hbm-smoke":
            continue
        key = (mode, row["scenario"], row["scheme"])
        assert row["seed"] not in groups[key]
        groups[key][row["seed"]] = row
        requests = tuple(
            (r["id"], r["profile"], r["arrival_ns"], r["pages"]) for r in row["requests"]
        )
        configuration = tuple(
            row[field]
            for field in (
                "nominal_gib_s",
                "page_bytes",
                "staging_bytes",
                "cache_budget_bytes",
                "arena_bytes",
            )
        )
        trace_key = (mode if args.suite == "joint" else None, row["scenario"], row["seed"])
        signature = (requests, configuration)
        assert traces.setdefault(trace_key, signature) == signature
    if args.suite == "joint":
        assert set(groups) == {
            (mode, "mix-balanced", scheme)
            for mode in ("buffered", "direct")
            for scheme in JOINT_COMPARISON
        }
    else:
        assert counts == {"buffered": 69, "direct": 24, "hbm-smoke": 3}, counts
    assert all(set(group) == {42, 43, 44} for group in groups.values())
    save(
        args.output / "metrics.csv",
        [summarize(k, list(v.values())) for k, v in sorted(groups.items())],
    )
    save(args.output / "paired.csv", list(comparisons(groups)))
    print(dict(counts))


if __name__ == "__main__":
    main()
