"""Build GitHub Pages from checked-in hardware results using only stdlib."""

import argparse
import html
import json
import math
import shutil
from pathlib import Path
from statistics import median


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NAMES = {
    "native": "原生加载", "hixl": "HIXL", "strata": "Strata", "bidaw": "Bidaw",
    "solid": "SolidAttention", "native-lru": "原生 + LRU", "strata-io": "Strata 仅布局",
    "solid-io": "SolidAttention 仅布局",
}
PROFILES = {
    "interactive-prefix": {
        "title": "多轮对话前缀",
        "about": "每批 4 个同时到达的查询，访问共享系统前缀和不同长度的会话历史。",
        "controls": ("strata-io", "native-lru"),
    },
    "sparse-decode": {
        "title": "稀疏解码",
        "about": "每步读取 10 个 KV 页。两组历史交替，动态选择随查询变化，步间隔固定为 4 ms。",
        "controls": ("native-lru", "solid-io"),
    },
}
MEASURED = ("native", "strata", "bidaw", "solid")
FIELDS = ("p50_ms", "p95_ms", "p99_ms", "ready_gib_s", "reads_per_request",
          "read_bytes_per_logical_byte", "hbm_bytes_per_logical_byte")
LABELS = ("P50 ms", "P95 ms", "P99 ms", "逻辑 GiB/s", "读调用/请求", "读/逻辑字节", "HBM/逻辑字节")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(result, path, key):
    if result.get("schema") != 1 or result.get("backend") != "ascend":
        raise ValueError(f"expected hardware schema 1 in {path}")
    if (result["io"], result["profile"], result["scheme"]) != key or result["status"] != "ok":
        raise ValueError(f"unexpected result in {path}")
    if len(result["trials"]) != result["repeat"] or not result["trials"]:
        raise ValueError(f"invalid trials in {path}")
    for trial in result["trials"]:
        if trial["counters"].get("verified_pages", 0) <= 0:
            raise ValueError(f"missing device verification in {path}")
        if len(trial["requests"]) != trial["summary"]["requests"] or len(trial["waves"]) != result["waves"]:
            raise ValueError(f"bad sample counts in {path}")
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
            for scheme in (*MEASURED, *metadata["controls"], "hixl"):
                path = results / mode / f"{profile}.{scheme}.json"
                if scheme == "hixl" and not path.exists():
                    continue
                result = read_json(path)
                key = (mode, profile, scheme)
                validate(result, path, key)
                matrix[key] = result
    for mode in ("buffered", "direct"):
        for profile in PROFILES:
            base = matrix[mode, profile, "native"]
            for (cell_mode, cell_profile, scheme), cell in matrix.items():
                if (cell_mode, cell_profile) != (mode, profile) or scheme == "hixl":
                    continue
                for field in ("geometry", "corpus_seed", "trace_seed", "waves", "repeat", "warmup", "storage"):
                    if cell[field] != base[field]:
                        raise ValueError(f"mismatched {field} for {mode}/{profile}/{scheme}")
    hixl = read_json(results / "hixl-loopback.json")
    if hixl.get("scheme") != "hixl" or hixl.get("backend") != "ascend" or hixl.get("status") != "blocked":
        raise ValueError("expected the Ascend same-device HIXL probe")
    return matrix, hixl


def chip_table(matrix, mode, profile, schemes, hixl):
    rows = []
    for scheme in schemes:
        cell = matrix.get((mode, profile, scheme))
        if not cell:
            message = f'Connect {hixl["hixl_status"]} · 需独立端点'
            rows.append(f'<tr><th scope="row">HIXL</th><td class="missing" colspan="7">{html.escape(message)}</td></tr>')
            continue
        summary = cell["summary"]
        p95 = [trial["summary"]["p95_ms"] for trial in cell["trials"]]
        hint = f"各轮 P95 范围 {min(p95):.2f}–{max(p95):.2f} ms"
        values = []
        for field in FIELDS:
            value = summary[field]
            label = f'<abbr title="{hint}">{value:.2f}</abbr>' if field == "p95_ms" else f"{value:.2f}"
            values.append(f"<td>{label}</td>")
        row_class = ' class="control"' if scheme not in (*MEASURED, "hixl") else ""
        rows.append(f'<tr{row_class}><th scope="row">{NAMES[scheme]}</th>{"".join(values)}</tr>')
    cols = "".join(f'<th scope="col">{label}</th>' for label in ("方案", *LABELS))
    caption = "Buffered I/O 实测与消融" if mode == "buffered" else "Direct I/O 敏感性试验"
    return (
        f'<div class="table-wrap" role="region" aria-label="{caption}" tabindex="0"><table>'
        f'<caption>{caption}</caption><thead><tr>{cols}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def chart(matrix, profile):
    data = [(scheme, matrix["buffered", profile, scheme]["summary"])
            for scheme in (*MEASURED, "hixl") if ("buffered", profile, scheme) in matrix]
    high = max(row["p95_ms"] for _, row in data)
    rows = []
    for scheme, row in data:
        p95, gib = row["p95_ms"], row["ready_gib_s"]
        rows.append(
            f'<div class="bar-row" data-scheme="{scheme}" data-ms="{p95:.5f}" data-gib="{gib:.5f}">'
            f'<span>{NAMES[scheme]}</span><div class="bar-rail" aria-hidden="true">'
            f'<div class="bar-fill" style="--size:{p95 / high * 100:.2f}%"></div></div>'
            f'<span class="value">{p95:.2f}</span></div>'
        )
    return (
        '<div class="chart"><div class="chart-head">'
        '<strong data-metric-title>P95 就绪延迟 (ms) · 越低越好</strong>'
        '<div class="metric" aria-label="图表指标" hidden>'
        '<button type="button" data-metric="ms" aria-pressed="true">延迟</button>'
        '<button type="button" data-metric="gib" aria-pressed="false">吞吐</button></div></div>'
        f'<div class="bars">{"".join(rows)}</div></div>'
    )


def panels(matrix, hixl):
    sections = []
    for profile, meta in PROFILES.items():
        schemes = ("native", "strata", "bidaw", "solid", "hixl", *meta["controls"])
        buf = matrix["buffered", profile, "native"]
        direct = matrix["direct", profile, "native"]
        n, nd = buf["summary"]["requests"], direct["summary"]["requests"]
        sections.append(
            f'<article class="profile" data-profile="{profile}" aria-labelledby="{profile}-title">'
            f'<h3 id="{profile}-title">{meta["title"]}</h3><p class="blurb">{meta["about"]}</p>'
            f'{chart(matrix, profile)}{chip_table(matrix, "buffered", profile, schemes, hixl)}'
            f'<p class="sample">主实验每轮 {n:g} 个请求，共 {buf["repeat"]} 轮。'
            '表中 p95 可查看跨轮范围，所有方案的尾批数据均在 HBM 中回读校验。</p>'
            '<details><summary>展开 Direct I/O 对照</summary>'
            f'{chip_table(matrix, "direct", profile, schemes, hixl)}'
            f'<p class="sample">每轮 {nd:g} 个请求，共 {direct["repeat"]} 轮。'
            '样本量较小，尾部数据用于观察 I/O 模式敏感性。</p></details></article>'
        )
    return "\n".join(sections)


def insights(matrix, hixl):
    dense, sparse = "interactive-prefix", "sparse-decode"

    def metric(profile, scheme, field, mode="buffered"):
        return matrix[mode, profile, scheme]["summary"][field]

    native = metric(dense, "native", "p95_ms")
    native_direct = metric(dense, "native", "p95_ms", "direct")
    strata = metric(dense, "strata", "p95_ms")
    strata_direct = metric(dense, "strata", "p95_ms", "direct")
    stage = matrix["buffered", dense, "native"]
    disk_ms = median(t["counters"]["disk_ns"] / t["summary"]["requests"] / 1e6 for t in stage["trials"])
    h2d_ms = median(t["counters"]["h2d_ns"] / t["summary"]["requests"] / 1e6 for t in stage["trials"])
    hbm_saved = 1 - metric(dense, "strata", "hbm_bytes_per_logical_byte")
    native_calls = metric(dense, "native", "reads_per_request")
    strata_calls = metric(dense, "strata-io", "reads_per_request")
    bidaw_hit = metric(dense, "bidaw", "host_hit_fraction")
    lru_hit = metric(dense, "native-lru", "host_hit_fraction")
    sparse_p95 = metric(sparse, "solid", "p95_ms")
    sparse_control = metric(sparse, "solid-io", "p95_ms")
    solid = matrix["buffered", sparse, "solid"]
    prefetch_wait = median(
        t["counters"]["prefetch_wait_ns"] / t["summary"]["requests"] / 1e6 for t in solid["trials"]
    )
    articles = [
        ("原生路径的成本",
         f"多轮前缀下，Buffered 每请求平均文件读取 {disk_ms:.2f} ms，H2D {h2d_ms:.2f} ms。"
         f"改用 Direct I/O 时，KV 就绪 P95 从 {native:.2f} ms 变为 {native_direct:.2f} ms。"
         "这里的 H2D 使用普通 DRAM，不同主机缓冲策略会影响这个阶段。"),
        ("Strata 合并了重复回载",
         f"仅改变到 page-first 布局时，文件读调用由每请求 {native_calls:.1f} 次降至 {strata_calls:.1f} 次。"
         f"加入共享后，HBM 的搬运字节减少 {hbm_saved:.1%}，Buffered P95 从 {native:.2f} ms 变为 {strata:.2f} ms，"
         f"Direct P95 从 {native_direct:.2f} ms 变为 {strata_direct:.2f} ms。"),
        ("Bidaw 与等容量 LRU",
         f"多轮前缀的 DRAM 命中率从 LRU 的 {lru_hit:.1%} 变为 {bidaw_hit:.1%}。"
         f"Buffered P95 分别为 {metric(dense, 'native-lru', 'p95_ms'):.2f} 和 {metric(dense, 'bidaw', 'p95_ms'):.2f} ms，"
         f"Direct P95 分别为 {metric(dense, 'native-lru', 'p95_ms', 'direct'):.2f} 和 "
         f"{metric(dense, 'bidaw', 'p95_ms', 'direct'):.2f} ms。结果取决于回复长度与返访距离的相关性。"),
        ("SolidAttention 的预取代价",
         f"稀疏解码中，仅交织 K/V 将每请求读调用从 {metric(sparse, 'native', 'reads_per_request'):.1f} 降至 "
         f"{metric(sparse, 'solid-io', 'reads_per_request'):.1f} 次。"
         f"加入预取的有效率为 {metric(sparse, 'solid', 'prefetch_use_fraction'):.1%}，"
         f"每逻辑字节读 {metric(sparse, 'solid', 'read_bytes_per_logical_byte'):.2f} 字节。"
         f"4 ms 步间隔之后，平均仍多等待 {prefetch_wait:.2f} ms。"
         f"Buffered P95 从 {sparse_control:.2f} ms 变为 {sparse_p95:.2f} ms，Direct P95 从 "
         f"{metric(sparse, 'solid-io', 'p95_ms', 'direct'):.2f} ms 变为 {metric(sparse, 'solid', 'p95_ms', 'direct'):.2f} ms。"),
        ("HIXL 双机性能待测",
         f"单设备探针在 {hixl['phase']} 返回 {hixl['hixl_status']}。独立端点可用后，"
         "按同样的语料记录存储端文件读、控制与整理开销、HIXL 传输及 HBM 校验，"
         "并用实际端口计数确认数据路径。"),
    ]
    return '<div class="insights">' + "".join(
        f"<article><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></article>" for title, body in articles
    ) + "</div>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=ROOT / "results/ascend-910b2")
    ap.add_argument("--output", type=Path, default=ROOT / "build/site")
    args = ap.parse_args()
    matrix, hixl = load(args.results)
    static = args.output / "assets"
    static.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "style.css", static / "style.css")
    shutil.copyfile(HERE / "report.js", static / "report.js")
    output = {"schema": 1, "runs": [], "hixl_same_device": hixl}
    for cell in matrix.values():
        output["runs"].append({k: v for k, v in cell.items() if k != "trials"})
    (static / "summary.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    template = (HERE / "index.html").read_text(encoding="utf-8")
    report = template.replace("@@RESULTS@@", panels(matrix, hixl)).replace("@@INSIGHTS@@", insights(matrix, hixl))
    (args.output / "index.html").write_text(report, encoding="utf-8")
    print(f"Built {args.output / 'index.html'} from {len(matrix)} measured cells and a HIXL probe")


if __name__ == "__main__":
    main()
