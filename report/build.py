"""Build the report and vector figures from released hardware results."""

import argparse
import html
import json
import math
import shutil
from pathlib import Path
from statistics import median

import mixed
import prefix
from figures import render, stage_values
from release_data import ResultsRelease


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
MIXED_RESULTS = HERE / "mixed-results.csv"
JOINT_RESULTS = HERE / "results/joint-four-baselines/raw-results.tar.gz"
PROFILES = {
    "interactive-prefix": {
        "title": "多轮对话前缀",
        "controls": ("strata-io", "native-lru"),
    },
    "sparse-decode": {
        "title": "稀疏解码",
        "controls": ("native-lru", "solid-io"),
    },
}
MEASURED = ("native", "roce", "strata", "bidaw", "solid")
FIELDS = ("p50_ms", "p95_ms", "p99_ms", "ready_gib_s", "reads_per_request",
          "read_bytes_per_logical_byte", "hbm_bytes_per_logical_byte")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(result, path, key):
    if result.get("schema") != 1 or result.get("backend") != "ascend":
        raise ValueError(f"expected hardware schema 1 in {path}")
    if (result["io"], result["profile"], result["scheme"]) != key or result["status"] != "ok":
        raise ValueError(f"unexpected result in {path}")
    if len(result["trials"]) != result["repeat"] or not result["trials"]:
        raise ValueError(f"invalid trials in {path}")
    if key[-1] == "roce" and result.get("roce", {}).get("control_loopback") is not True:
        raise ValueError(f"this report expects a same-node RoCE measurement in {path}")
    for trial in result["trials"]:
        if trial["counters"].get("verified_pages", 0) <= 0:
            raise ValueError(f"missing device verification in {path}")
        if len(trial["requests"]) != trial["summary"]["requests"] or len(trial["waves"]) != result["waves"]:
            raise ValueError(f"bad sample counts in {path}")
        if key[-1] == "roce":
            c = trial["counters"]
            if not (c["roce_ns"] > 0 and c["roce_bytes"] == c["hbm_bytes"] == c["storage_bytes"] > 0):
                raise ValueError(f"inconsistent RoCE transfer in {path}")
    for field, value in result["summary"].items():
        if not math.isfinite(value) or not math.isclose(value, median(t["summary"][field] for t in result["trials"])):
            raise ValueError(f"invalid summary for {field} in {path}")
    for field in FIELDS:
        if field not in result["summary"]:
            raise ValueError(f"missing {field} in {path}")


def load(results):
    matrix = {}
    for mode in ("buffered", "direct"):
        for profile, metadata in PROFILES.items():
            for scheme in (*MEASURED, *metadata["controls"]):
                path = results / mode / f"{profile}.{scheme}.json"
                result = read_json(path)
                key = (mode, profile, scheme)
                validate(result, path, key)
                matrix[key] = result
    for mode in ("buffered", "direct"):
        for profile in PROFILES:
            base = matrix[mode, profile, "native"]
            if matrix[mode, profile, "bidaw"]["run"] != matrix[mode, profile, "native-lru"]["run"]:
                raise ValueError(f"Bidaw and LRU must share a measurement run for {mode}/{profile}")
            for (cell_mode, cell_profile, scheme), cell in matrix.items():
                if (cell_mode, cell_profile) != (mode, profile):
                    continue
                for field in ("geometry", "corpus_seed", "trace_seed", "waves", "repeat", "warmup", "storage"):
                    if cell[field] != base[field]:
                        raise ValueError(f"mismatched {field} for {mode}/{profile}/{scheme}")
    return matrix


def figure(name, alt, *, mobile=None):
    safe_alt = html.escape(alt)
    source = f'<source media="(max-width: 48rem)" srcset="figures/{mobile}.svg">' if mobile else ""
    return (
        '<figure class="paper-figure"><picture>'
        f'{source}<img src="figures/{name}.svg" alt="{safe_alt}" decoding="async">'
        '</picture></figure>'
    )


def panels():
    return "\n".join(
        f'<article class="profile" data-profile="{profile}" aria-label="{meta["title"]}">'
        + figure(f"{profile}-performance", f"{meta['title']}的 P95 延迟和逻辑吞吐")
        + '</article>'
        for profile, meta in PROFILES.items()
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, help="Use a local mechanism results directory")
    ap.add_argument("--prefix-results", type=Path, help="Use a local Agent prefix results directory")
    ap.add_argument("--output", type=Path, default=ROOT / "build/site")
    args = ap.parse_args()
    release = None
    if args.results is None:
        release = ResultsRelease.read(HERE / "results-release.json")
        with release.unpack(ROOT / "build" / "results") as directory:
            matrix = load(directory)
    else:
        matrix = load(args.results)
    if args.prefix_results is None:
        prefix_release = ResultsRelease.read(HERE / "prefix-results-release.json")
        with prefix_release.unpack(ROOT / "build" / "results") as directory:
            prefix_plan, prefix_runs = prefix.read(directory)
    else:
        prefix_plan, prefix_runs = prefix.read(args.prefix_results)
    prefix_summary = prefix.summarize(prefix_plan, prefix_runs)
    prefix.render(prefix_summary, prefix_runs, args.output / "figures")
    joint_runs = mixed.load_joint(JOINT_RESULTS)
    figure_dir = args.output / "figures"
    render(matrix, figure_dir)
    mixed.render_joint(joint_runs, figure_dir)
    static = args.output / "assets"
    static.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STATIC / "style.css", static / "style.css")
    shutil.copyfile(STATIC / "report.js", static / "report.js")
    shutil.copyfile(MIXED_RESULTS, static / "mixed-results.csv")
    mixed.write_joint_csv(joint_runs, static / "joint-mixed-trials.csv")
    output = {"schema": 1, "runs": []}
    if release is not None:
        output["results_release"] = {"tag": release.tag, "url": release.page_url, "sha256": release.sha256}
    for cell in matrix.values():
        row = {k: v for k, v in cell.items() if k != "trials"}
        if cell["scheme"] in ("native", "roce"):
            keys = ("file_read", "local_staging", "control", "rc_and_staging", "h2d")
            row["stage_mean_ms_per_request"] = dict(zip(keys, stage_values(cell)))
        output["runs"].append(row)
    (static / "summary.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (static / "prefix-summary.json").write_text(
        json.dumps(prefix_summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    template = (STATIC / "index.html").read_text(encoding="utf-8")
    detail = "\n".join((
        figure("latency-ecdf", "两种负载的请求级延迟分布"),
        figure("mechanism-ablation", "三种论文机制的配对消融"),
        figure("roce-stages", "原生与 RoCE 的逐阶段开销"),
    ))
    report = (
        template.replace("@@RESULTS@@", panels())
        .replace("@@PREFIX_RESULTS@@", figure(
            "tool-agent-prefix", "Agent 前缀回载共现率与小回载就绪延迟", mobile="tool-agent-prefix-mobile",
        ))
        .replace("@@JOINT_RESULTS@@", mixed.joint_dashboard())
        .replace("@@FIGURES@@", detail)
    )
    (args.output / "index.html").write_text(report, encoding="utf-8")
    print(f"Built {args.output / 'index.html'} and vector figures from {len(matrix)} measured cells")


if __name__ == "__main__":
    main()
