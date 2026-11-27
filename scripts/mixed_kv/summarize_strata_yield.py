"""Validate and summarize matched-rate Strata and cooperative-yield trials."""
import argparse
import csv
import json
import math
import tarfile
from collections import defaultdict
from pathlib import Path
from statistics import mean, median


PREFIX = "interactive-prefix"
SPARSE = "sparse-decode"
BLOCKING = ("prefix_read_ns", "prefix_h2d_ns", "sparse_read_ns", "sparse_h2d_ns")


def quantile(xs, fraction):
    values = sorted(xs)
    i = (len(values) - 1) * fraction
    lo = math.floor(i)
    return values[lo] + (i - lo) * (values[math.ceil(i)] - values[lo])


def read_archive(path):
    with tarfile.open(path, "r:gz") as archive:
        for entry in archive:
            name = Path(entry.name)
            if entry.isfile() and name.suffix == ".json":
                with archive.extractfile(entry) as f:
                    yield name.parent.name, json.load(f)


def validate(row, full):
    stats, requests, intervals = row["counts"], row["requests"], row["stage_intervals"]
    assert row["backend"] == "ascend"
    assert stats["verified_pages"] > 0
    if full:
        assert stats["verified_pages"] * row["page_bytes"] == stats["logical_bytes"]
    assert stats["wall_ns"] == max(r["ready_ns"] for r in requests)
    logical = sum(len(r["pages"]) for r in requests) * row["page_bytes"]
    assert logical == stats["logical_bytes"]
    assert (
        stats["hbm_bytes"]
        == stats["storage_bytes"]
        == logical - stats.get("shared_pages_saved", 0) * row["page_bytes"]
    )
    if row["policy"] != "h2d-pipeline":
        assert all(a[3] <= b[2] for a, b in zip(intervals, intervals[1:]))
    for phase in ("read", "h2d"):
        ordered = [i for i in intervals if i[1] == phase]
        assert all(a[3] <= b[2] for a, b in zip(ordered, ordered[1:]))
    assert all(r["arrival_ns"] <= r["start_ns"] <= r["ready_ns"] for r in requests)
    for key in ("read", "h2d"):
        assert stats[f"{key}_ns"] == sum(
            stop - start for _, phase, start, stop in intervals if phase == key
        )
    for kind, measured in row["classes"].items():
        records = [r for r in requests if r["profile"] == kind]
        assert len(records) == measured["requests"]
        assert math.isclose(
            quantile([(r["ready_ns"] - r["arrival_ns"]) / 1e6 for r in records], 0.95),
            measured["p95_ms"],
        )
        if kind == SPARSE and row["policy"] != "h2d-pipeline":
            for r in records:
                blocking = defaultdict(int)
                for owner, phase, start, stop in intervals:
                    overlap = min(stop, r["start_ns"]) - max(start, r["arrival_ns"])
                    if overlap > 0:
                        label = "prefix" if owner == PREFIX else "sparse"
                        blocking[f"{label}_{phase}_ns"] += overlap
                assert dict(blocking) == r["blocking"]
                assert sum(blocking.values()) <= r["start_ns"] - r["arrival_ns"]


def tail_breakdown(trial):
    sparse = [r for r in trial["requests"] if r["profile"] == SPARSE]
    tail = sorted(sparse, key=lambda r: r["ready_ns"] - r["arrival_ns"])[
        -math.ceil(len(sparse) / 10) :
    ]
    counts = {
        "total_ns": mean(r["ready_ns"] - r["arrival_ns"] for r in tail),
        "wait_ns": mean(r["start_ns"] - r["arrival_ns"] for r in tail),
        "other_wait_ns": mean(
            r["start_ns"] - r["arrival_ns"] - sum(r["blocking"].values()) for r in tail
        )
        if trial["policy"] != "h2d-pipeline"
        else 0,
    }
    for field in BLOCKING:
        counts[field] = mean(r["blocking"].get(field, 0) for r in tail)
    for field in ("read_ns", "h2d_ns"):
        counts[f"own_{field}"] = mean(r[field] for r in tail)
    counts["own_h2d_wait_ns"] = mean(r.get("h2d_wait_ns", 0) for r in tail)
    return counts


def metrics(trials, key):
    batch, io, scenario, policy, rate, quantum = key
    row = {
        "batch": batch,
        "io": io,
        "scenario": scenario,
        "policy": policy,
        "rate": rate,
        "quantum": quantum,
        "runs": len(trials),
    }
    for kind, label in ((PREFIX, "prefix"), (SPARSE, "sparse")):
        values = [t["classes"][kind] for t in trials if kind in t["classes"]]
        for field in ("p95_ms", "queue_p95_ms"):
            row[f"{label}_{field}"] = median(r[field] for r in values) if values else None
    row["logical_gib_s"] = median(r["logical_gib_s"] for r in trials)
    for field in (
        "read_calls",
        "storage_bytes",
        "hbm_bytes",
        "shared_pages_saved",
        "sparse_yields",
        "copies",
    ):
        row[field] = median(r["counts"].get(field, 0) for r in trials)
    tail = [tail_breakdown(r) for r in trials]
    for field in tail[0]:
        row["tail_" + field.replace("_ns", "_ms")] = mean(t[field] for t in tail) / 1e6
    return row


def comparisons(grouped):
    for key, candidate in sorted(grouped.items()):
        batch, io, scenario, policy, rate, quantum = key
        if scenario not in ("mix-balanced", "mix-sparse-heavy"):
            continue
        if policy == "strata":
            control = (batch, io, "sparse-matched", policy, rate, quantum)
        else:
            control = (batch, io, scenario, "strata", rate, quantum)
        if control not in grouped or control == key:
            continue
        by_seed = {r["seed"]: r for r in grouped[control]}
        for b in candidate:
            a = by_seed[b["seed"]]
            if control[2] == "sparse-matched":

                def sparse_pages(run):
                    return [r["pages"] for r in run["requests"] if r["profile"] == SPARSE]

                assert sparse_pages(a) == sparse_pages(b)
            row = {
                "batch": batch,
                "io": io,
                "scenario": scenario,
                "policy": policy,
                "rate": rate,
                "quantum": quantum,
                "baseline": control[2] + "/" + control[3],
                "seed": b["seed"],
            }
            for label, kind in (("prefix", PREFIX), ("sparse", SPARSE)):
                if kind in a["classes"] and kind in b["classes"]:
                    old = a["classes"][kind]["p95_ms"]
                    new = b["classes"][kind]["p95_ms"]
                    row[f"{label}_change_pct"] = (new / old - 1) * 100
                else:
                    row[f"{label}_change_pct"] = None
            rate_change = (b["logical_gib_s"] / a["logical_gib_s"] - 1) * 100
            row["throughput_change_pct"] = rate_change
            yield row


def output_file(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    grouped = defaultdict(list)
    traces = {}
    count = 0
    for batch, row in read_archive(args.archive):
        count += 1
        validate(row, batch in ("hbm-check", "hbm-pipeline-check"))
        if batch in ("hbm-check", "hbm-pipeline-check"):
            continue
        trace = tuple((r["profile"], r["arrival_ns"], r["pages"]) for r in row["requests"])
        key = (row["scenario"], row["seed"], row["nominal_mix_gib_s"], row["scale"])
        assert traces.setdefault(key, trace) == trace
        grouped[
            (
                batch,
                row["io"],
                row["scenario"],
                row["policy"],
                row["nominal_mix_gib_s"],
                row["quantum_pages"],
            )
        ].append(row)
    assert all(len({r["seed"] for r in trials}) == len(trials) for trials in grouped.values())
    args.output.mkdir(parents=True, exist_ok=True)
    output_file(
        args.output / "metrics.csv", [metrics(rows, key) for key, rows in sorted(grouped.items())]
    )
    output_file(args.output / "paired.csv", list(comparisons(grouped)))
    print(f"Validated {count} trials in {len(grouped)} measured cells")


if __name__ == "__main__":
    main()
