"""Verify and summarize the mixed-load raw records exported from an experiment."""
import argparse
import csv
import json
import math
import tarfile
from collections import defaultdict
from pathlib import Path
from statistics import median

PREFIX = "interactive-prefix"
SPARSE = "sparse-decode"


def quantile(values, fraction):
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo = math.floor(index)
    return ordered[lo] * (1 - (index - lo)) + ordered[math.ceil(index)] * (index - lo)


def load(root):
    if root.is_dir():
        for mode in ("buffered", "direct", "direct-low", "hbm-smoke"):
            for path in sorted((root / mode).glob("*.json")):
                yield mode, json.loads(path.read_text(encoding="utf-8"))
    else:
        with tarfile.open(root, "r:gz") as archive:
            for member in archive:
                path = Path(member.name)
                if (
                    path.parent.name in ("buffered", "direct", "direct-low", "hbm-smoke")
                    and path.suffix == ".json"
                    and member.isfile()
                ):
                    with archive.extractfile(member) as data:
                        yield path.parent.name, json.load(data)


def validate(mode, row):
    requests, chunks, c = row["requests"], row["chunks"], row["counts"]
    page_bytes = row["page_bytes"]
    assert row["backend"] == "ascend"
    assert requests and chunks and 0 < c["peak_slots"] <= row["slots"] == 2
    assert c["wall_ns"] == max(chunk["completed_ns"] for chunk in chunks)
    assert c["verified_pages"] > 0
    assert c["logical_bytes"] == sum(len(req["pages"]) * page_bytes for req in requests)
    assert (
        c["storage_bytes"]
        == c["hbm_bytes"]
        == sum(len(chunk["pages"]) * page_bytes for chunk in chunks)
    )
    if row["transport"] == "roce":
        assert c["roce_bytes"] == c["storage_bytes"] and c["roce_ns"] > 0
    else:
        assert not c.get("roce_bytes", 0)
    for req in requests:
        assert req["arrival_ns"] <= req["first_ns"] <= req["ready_ns"] <= c["wall_ns"]
    for chunk in chunks:
        assert chunk["issued_ns"] <= chunk["ready_ns"] <= chunk["completed_ns"]
        assert 0 < len(chunk["pages"]) * page_bytes <= row["slot_bytes"]
    for field, measured in row["classes"].items():
        group = [r for r in requests if r["profile"] == field]
        assert measured["requests"] == len(group)
        p95 = quantile([(r["ready_ns"] - r["arrival_ns"]) / 1e6 for r in group], 0.95)
        assert math.isclose(measured["p95_ms"], p95, rel_tol=1e-12)
        assert measured["on_time"] == sum(r["ready_ns"] <= r["deadline_ns"] for r in group) / len(
            group
        )
    if mode == "hbm-smoke":
        assert c["verified_pages"] * page_bytes == c["logical_bytes"]


def aggregate(mode, trials):
    trial, *_ = trials
    cls = (trial["io"], trial["transport"], trial["scenario"], trial["policy"])
    result = dict(zip(("io", "transport", "scenario", "policy"), cls))
    result["condition"] = mode
    result["nominal_mix_gib_s"] = trial["nominal_mix_gib_s"]
    result["trials"] = len(trials)
    for name, profile in (("prefix", PREFIX), ("sparse", SPARSE)):
        samples = [t["classes"][profile] for t in trials if profile in t["classes"]]
        result[f"{name}_requests_per_trial"] = samples[0]["requests"] if samples else None
        for metric in ("p95_ms", "queue_p95_ms", "on_time"):
            result[f"{name}_{metric}"] = median(s[metric] for s in samples) if samples else None
        for tag, aggregate_range in (("min", min), ("max", max)):
            result[f"{name}_p95_{tag}_ms"] = (
                aggregate_range(s["p95_ms"] for s in samples) if samples else None
            )
    result["logical_gib_s"] = median(t["ready_gib_s"] for t in trials)
    result["physical_read_ratio"] = median(
        t["counts"]["storage_bytes"] / t["counts"]["logical_bytes"] for t in trials
    )
    for key in ("disk_ns", "control_ns", "roce_ns", "h2d_ns", "h2d_queue_ns"):
        result[key.removesuffix("_ns") + "_ms_per_chunk"] = median(
            t["counts"].get(key, 0) / len(t["chunks"]) / 1e6 for t in trials
        )
    for key in ("fetch_ns", "h2d_ns"):
        result[key.removesuffix("_ns") + "_busy_fraction"] = median(
            t["counts"][key] / t["counts"]["wall_ns"] for t in trials
        )
    result["merged_pages_saved"] = median(t["counts"]["shared_transfers_saved"] for t in trials)
    result["prefetch_pages"] = median(
        t["counts"]["prefetch_bytes"] / t["page_bytes"] for t in trials
    )
    result["prefetch_prearrival_hits"] = median(
        sum(r["prefetch_hits"] for r in t["requests"]) for t in trials
    )
    result["prefetch_late_deliveries"] = median(
        sum(r["late_prefetch_hits"] for r in t["requests"]) for t in trials
    )
    result["feedback_hold_ms"] = median(
        t["counts"].get("feedback_wait_ns", 0) / 1e6 for t in trials
    )
    result["verified_pages"] = sum(t["counts"]["verified_pages"] for t in trials)
    return result


def compare(grouped):
    cases = []
    for transport in ("local", "roce"):
        for profile, name in ((PREFIX, "prefix"), (SPARSE, "sparse")):
            for solo in (f"{name}-only", f"{name}-matched"):
                cases.append(
                    (
                        profile,
                        ("buffered", transport, solo, "fifo"),
                        ("buffered", transport, "mix-balanced", "fifo"),
                    )
                )
    for io in ("buffered", "direct", "direct-low"):
        for scenario in ("mix-balanced", "mix-sparse-heavy"):
            for control in (
                "fifo",
                "static",
                "ingress-edf",
                "feedback-no-merge",
                "feedback-no-prefetch",
            ):
                for profile in (PREFIX, SPARSE):
                    cases.append(
                        (
                            profile,
                            (io, "roce", scenario, control),
                            (io, "roce", scenario, "feedback"),
                        )
                    )
    for profile in (PREFIX, SPARSE):
        cases.append(
            (
                profile,
                ("buffered", "local", "mix-balanced", "fifo"),
                ("buffered", "roce", "mix-balanced", "fifo"),
            )
        )
        for policy in ("fifo", "static", "ingress-edf", "feedback"):
            cases.append(
                (
                    profile,
                    ("direct", "roce", "mix-balanced", policy),
                    ("direct-low", "roce", "mix-balanced", policy),
                )
            )
    for profile, before, after in cases:
        if before not in grouped or after not in grouped:
            continue
        for seed in sorted(grouped[before].keys() & grouped[after].keys()):
            a, b = grouped[before][seed], grouped[after][seed]
            ma, mb = (d["classes"][profile] for d in (a, b))
            yield {
                "seed": seed,
                "profile": profile,
                "baseline": "/".join(before),
                "comparison": "/".join(after),
                "baseline_p95_ms": ma["p95_ms"],
                "comparison_p95_ms": mb["p95_ms"],
                "p95_change_pct": (mb["p95_ms"] / ma["p95_ms"] - 1) * 100,
                "baseline_on_time": ma["on_time"],
                "comparison_on_time": mb["on_time"],
                "baseline_gib_s": a["ready_gib_s"],
                "comparison_gib_s": b["ready_gib_s"],
            }


def save(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    experiments = defaultdict(dict)
    traces = {}
    raw_counts = defaultdict(int)
    for mode, trial in load(args.input):
        validate(mode, trial)
        raw_counts[mode] += 1
        if mode == "hbm-smoke":
            continue
        setup = (mode, trial["transport"], trial["scenario"], trial["policy"])
        seed = trial["seed"]
        assert seed not in experiments[setup]
        experiments[setup][seed] = trial
        trace = tuple((r["arrival_ns"], r["pages"], r["profile"]) for r in trial["requests"])
        key = (mode == "direct-low", trial["scenario"], seed)
        assert traces.setdefault(key, trace) == trace
    if args.full:
        assert dict(raw_counts) == {
            "buffered": 54,
            "direct": 12,
            "direct-low": 12,
            "hbm-smoke": 2,
        }, dict(raw_counts)
        assert all(len(v) == 3 for v in experiments.values())
    args.output.mkdir(parents=True, exist_ok=True)
    save(
        args.output / "metrics.csv",
        [aggregate(key[0], list(v.values())) for key, v in sorted(experiments.items())],
    )
    save(args.output / "paired-comparisons.csv", list(compare(experiments)))
    print(dict(raw_counts))


if __name__ == "__main__":
    main()
