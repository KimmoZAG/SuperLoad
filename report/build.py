"""Build the report and vector figures from checked-in hardware results."""

import argparse
import html
import json
import math
import shutil
from pathlib import Path
from statistics import median

import mixed
from figures import render, stage_values
from release_data import ResultsRelease


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
MIXED_RESULTS = HERE / "mixed-results.csv"
NAMES = {
    "native": "原生加载", "roce": "RoCE RC", "strata": "Strata", "bidaw": "Bidaw",
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
MEASURED = ("native", "roce", "strata", "bidaw", "solid")
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


def chip_table(matrix, mode, profile, schemes):
    rows = []
    for scheme in schemes:
        cell = matrix[mode, profile, scheme]
        summary = cell["summary"]
        p95 = [trial["summary"]["p95_ms"] for trial in cell["trials"]]
        hint = f"各轮 P95 范围 {min(p95):.2f}–{max(p95):.2f} ms"
        values = []
        for field in FIELDS:
            value = summary[field]
            label = f'<abbr title="{hint}">{value:.2f}</abbr>' if field == "p95_ms" else f"{value:.2f}"
            values.append(f"<td>{label}</td>")
        row_class = ' class="control"' if scheme not in MEASURED else ""
        rows.append(f'<tr{row_class}><th scope="row">{NAMES[scheme]}</th>{"".join(values)}</tr>')
    cols = "".join(f'<th scope="col">{label}</th>' for label in ("方案", *LABELS))
    caption = "Buffered I/O 实测与消融" if mode == "buffered" else "Direct I/O 敏感性试验"
    return (
        f'<div class="table-wrap" role="region" aria-label="{caption}" tabindex="0"><table>'
        f'<caption>{caption}</caption><thead><tr>{cols}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def figure(name, alt, caption):
    return (
        '<figure class="paper-figure">'
        f'<div class="figure-viewport" tabindex="0" role="region" aria-label="{html.escape(alt)}">'
        f'<img src="figures/{name}.svg" alt="{html.escape(alt)}" decoding="async"></div>'
        f'<figcaption><span>{html.escape(caption)}'
        '<span class="mobile-scroll-note">横向滑动可查看完整面板。</span></span><span class="figure-downloads">'
        f'<a href="figures/{name}.svg" aria-label="{html.escape(alt)} SVG">SVG</a>'
        f'<a href="figures/{name}.pdf" aria-label="{html.escape(alt)} PDF">PDF</a>'
        '</span></figcaption></figure>'
    )


def panels(matrix):
    sections = []
    for profile, meta in PROFILES.items():
        schemes = (*MEASURED, *meta["controls"])
        buf = matrix["buffered", profile, "native"]
        direct = matrix["direct", profile, "native"]
        n, nd = buf["summary"]["requests"], direct["summary"]["requests"]
        caption = (
            f"实心菱形和标签为各轮中位数，空心点为单轮，延迟横线为实测范围。Buffered 每轮 {n:g} 个请求、{buf['repeat']} 轮，"
            f"Direct I/O 每轮 {nd:g} 个请求、{direct['repeat']} 轮。左右同指标共用横轴，延迟使用对数刻度。"
        )
        plot = figure(f"{profile}-performance", f"{meta['title']}的 P95 延迟和逻辑吞吐", caption)
        sections.append(
            f'<article class="profile" data-profile="{profile}" aria-labelledby="{profile}-title">'
            f'<h3 id="{profile}-title">{meta["title"]}</h3><p class="blurb">{meta["about"]}</p>'
            f'{plot}<details><summary>展开精确数值与消融对照</summary>'
            f'{chip_table(matrix, "buffered", profile, schemes)}'
            f'{chip_table(matrix, "direct", profile, schemes)}'
            '<p class="sample">P95 的跨轮范围可在表中查看。每个方案都做了 HBM 回读。'
            'Direct I/O 样本量较小，尾部数据用于观察 I/O 模式敏感性。</p></details></article>'
        )
    return "\n".join(sections)


def insights(matrix):
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
    transport = matrix["buffered", dense, "roce"]
    transport_p95 = transport["summary"]["p95_ms"]
    transport_ms = stage_values(transport)[3]
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
        ("RoCE 回环的额外传输",
         f"多轮前缀 Buffered P95 从原生的 {native:.2f} ms 变为 {transport_p95:.2f} ms。"
         f"RC 传输与注册缓冲整理平均占 {transport_ms:.2f} ms/请求。"
         "测量范围是同节点双进程、相同网口和 32 KiB 注册窗口，跨机和直达 HBM 需要各自测量。"),
    ]
    return '<div class="insights">' + "".join(
        f"<article><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></article>" for title, body in articles
    ) + "</div>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, help="Use a local results directory instead of the pinned release")
    ap.add_argument("--output", type=Path, default=ROOT / "build/site")
    args = ap.parse_args()
    release = None
    if args.results is None:
        release = ResultsRelease.read(HERE / "results-release.json")
        with release.unpack(ROOT / "build" / "results") as directory:
            matrix = load(directory)
    else:
        matrix = load(args.results)
    mixed_runs = mixed.load(MIXED_RESULTS)
    render(matrix, args.output / "figures")
    static = args.output / "assets"
    static.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STATIC / "style.css", static / "style.css")
    shutil.copyfile(STATIC / "report.js", static / "report.js")
    shutil.copyfile(MIXED_RESULTS, static / "mixed-results.csv")
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
    template = (STATIC / "index.html").read_text(encoding="utf-8")
    detail = "\n".join((
        figure("latency-ecdf", "两种负载的请求级延迟分布",
               "横轴为对数刻度，四个面板使用相同尺度。虚线为 95% 分位参考。每条曲线汇集该配置的全部请求，"
               "同批请求可能相关。曲线的 95% 位置与逐轮 P95 中位数的定义不同。"),
        figure("mechanism-ablation", "三种论文机制的配对消融",
               "分别显示 Strata 的读调用、Bidaw 和等容量 LRU 的命中率、SolidAttention 在同一布局下的预取代价。"
               "每个点为逐轮统计的中位数。"),
        figure("roce-stages", "原生与 RoCE 的逐阶段开销",
               "每段为逐轮每请求均值再取平均。同一 I/O 模式的两个面板共用刻度。"
               "RC 段含注册窗口整理，堆叠值只覆盖加载阶段。"),
    ))
    raw_link = f' · <a href="{html.escape(release.page_url)}">原始样本（Release）</a>' if release else ""
    report = (template.replace("@@RESULTS@@", panels(matrix)).replace("@@INSIGHTS@@", insights(matrix))
              .replace("@@MIXED_RESULTS@@", mixed.panels(mixed_runs))
              .replace("@@FIGURES@@", detail).replace("@@RESULTS_RELEASE@@", raw_link))
    (args.output / "index.html").write_text(report, encoding="utf-8")
    print(f"Built {args.output / 'index.html'} and vector figures from {len(matrix)} measured cells")


if __name__ == "__main__":
    main()
