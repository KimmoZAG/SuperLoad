"""Publication figures made directly from the checked-in hardware samples."""

from statistics import mean

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullFormatter, PercentFormatter, ScalarFormatter


SCHEMES = {
    "native": ("Native", "#505963", "-"),
    "roce": ("RoCE RC", "#0072B2", "--"),
    "strata": ("Strata", "#008463", "-"),
    "bidaw": ("Bidaw", "#C34E00", "-."),
    "solid": ("SolidAttention", "#A44783", ":"),
}
MODES = {"buffered": "Buffered", "direct": "Direct I/O"}
PROFILES = {"interactive-prefix": "Conversation prefixes", "sparse-decode": "Sparse decode"}
INK = "#17242B"
MUTED = "#627078"
GRID = "#DFE6E7"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 11,
    "axes.titleweight": "semibold", "axes.edgecolor": "#A3B1B6", "axes.labelcolor": INK,
    "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
    "grid.color": GRID, "grid.linewidth": 0.65, "svg.fonttype": "path", "svg.hashsalt": "superload",
    "pdf.fonttype": 42, "savefig.facecolor": "white",
})


def save(fig, output, name):
    fig.savefig(output / f"{name}.svg", metadata={"Date": None, "Title": name})
    fig.savefig(output / f"{name}.pdf", metadata={"CreationDate": None, "Title": name})
    plt.close(fig)


def panel_title(ax, letter, title):
    ax.set_title(f"({letter})  {title}", loc="left", pad=9)


def metric_panel(ax, matrix, mode, profile, metric, axis_limits):
    labels = []
    for i, (scheme, (label, color, _)) in enumerate(SCHEMES.items()):
        labels.append(label)
        cell = matrix[mode, profile, scheme]
        samples = [t["summary"][metric] for t in cell["trials"]]
        center = cell["summary"][metric]
        if metric == "p95_ms":
            ax.hlines(i, min(samples), max(samples), color=color, alpha=0.38, linewidth=2)
        else:
            ax.barh(i, center, color=color, alpha=0.16, height=0.58, linewidth=0)
        for j, value in enumerate(samples):
            ax.scatter(value, i + (j - (len(samples) - 1) / 2) * 0.18,
                       facecolors="white", edgecolors=color, s=22, zorder=3)
        ax.scatter(center, i, marker="D", color=color, s=25, zorder=4)
        ax.annotate(f"{center:.2f}", (max(samples), i), xytext=(5, 0), textcoords="offset points",
                    va="center", color=color, fontsize=9, fontweight="semibold")
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_ylim(len(labels) - 0.45, -0.55)
    if metric == "p95_ms":
        ax.set_xscale("log")
        ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlim(*axis_limits)
    ax.grid(axis="x")
    ax.tick_params(axis="y", length=0)


def performance(matrix, output, profile):
    fig, axes = plt.subplots(2, 2, figsize=(10.1, 6.15))
    fig.subplots_adjust(left=0.136, right=0.984, top=0.875, bottom=0.155, wspace=0.50, hspace=0.70)
    fig.suptitle(PROFILES[profile], x=0.135, ha="left", fontsize=16, fontweight="bold", y=0.978)
    for i, (metric, label) in enumerate((
            ("p95_ms", "P95 KV-ready latency (ms, log scale)"),
            ("ready_gib_s", "Logical throughput (GiB/s)"),
    )):
        values = [t["summary"][metric] for mode in MODES for scheme in SCHEMES
                  for t in matrix[mode, profile, scheme]["trials"]]
        limits = (min(values) * 0.8, max(values) * 1.65) if i == 0 else (0, max(values) * 1.27)
        for j, (mode, mode_label) in enumerate(MODES.items()):
            ax = axes[i, j]
            panel_title(ax, "abcd"[i * 2 + j], mode_label)
            metric_panel(ax, matrix, mode, profile, metric, limits)
            ax.set_xlabel(label)
    med = Line2D([], [], marker="D", color=INK, ls="none", markersize=5, label="Median across trials")
    run = Line2D([], [], marker="o", markerfacecolor="white", markeredgecolor=MUTED,
                 ls="none", markersize=5, label="Individual trial")
    fig.legend(handles=[med, run], loc="lower center", bbox_to_anchor=(0.5, 0.035),
               frameon=False, ncol=2, fontsize=10)
    save(fig, output, f"{profile}-performance")


def ecdf(matrix, output):
    fig, axes = plt.subplots(2, 2, figsize=(10.1, 6.45), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.1, right=0.97, top=0.87, bottom=0.17, wspace=0.16, hspace=0.42)
    fig.suptitle("KV-ready latency distributions", x=0.10, ha="left", fontsize=16, fontweight="bold", y=0.978)
    all_ms = [row["latency_ms"] for (_, _, scheme), cell in matrix.items() if scheme in SCHEMES
              for trial in cell["trials"] for row in trial["requests"]]
    lo = min(all_ms) * 0.82
    hi = max(all_ms) * 1.16
    for i, (profile, name) in enumerate(PROFILES.items()):
        for j, (mode, mode_label) in enumerate(MODES.items()):
            ax = axes[i, j]
            count = sum(len(t["requests"]) for t in matrix[mode, profile, "native"]["trials"])
            panel_title(ax, "abcd"[i * 2 + j], f"{name} / {mode_label}")
            ax.axhline(0.95, color="#8C9A9E", lw=0.75, ls="--")
            for scheme, (_, color, line) in SCHEMES.items():
                samples = sorted(row["latency_ms"] for t in matrix[mode, profile, scheme]["trials"]
                                 for row in t["requests"])
                ax.step([lo, *samples, hi], [0, *(k / len(samples) for k in range(1, len(samples) + 1)), 1],
                        where="post", color=color, linestyle=line, linewidth=1.55)
            ax.set(xscale="log", xlim=(lo, hi), ylim=(0, 1.02), yticks=[0, 0.25, 0.50, 0.75, 0.95])
            ax.grid(axis="x", which="major")
            ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.xaxis.set_major_formatter(ScalarFormatter())
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.text(0.025, 0.81, f"n = {count} requests\nper scheme", transform=ax.transAxes,
                    color=MUTED, fontsize=8.8, va="top",
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.89, "pad": 2})
        axes[i, 0].set_ylabel("Requests at or below x")
    for ax in axes[-1]:
        ax.set_xlabel("KV-ready latency (ms, log scale)")
    handles = [Line2D([], [], color=color, linestyle=line, label=label) for label, color, line in SCHEMES.values()]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.028), frameon=False, ncol=5)
    save(fig, output, "latency-ecdf")


def ablations(matrix, output):
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.85))
    fig.subplots_adjust(left=0.10, right=0.98, top=0.77, bottom=0.24, wspace=0.39)
    fig.suptitle("Mechanism ablations", x=0.10, ha="left", fontsize=16, fontweight="bold", y=0.975)
    columns = (
        ("Strata / prefixes", "interactive-prefix", ("native", "strata-io", "strata"),
         ("Native", "Page-first", "+ sharing"), "reads_per_request", "File reads per request (log)"),
        ("Bidaw / prefixes", "interactive-prefix", ("native-lru", "bidaw"),
         ("LRU", "Bidaw"), "host_hit_fraction", "Host cache hit rate"),
        ("SolidAttention / sparse", "sparse-decode", ("solid-io", "solid"),
         ("Interleave", "+ prefetch"), "p95_ms", "P95 KV-ready latency (ms)"),
    )
    marks = (("buffered", "#0072B2", "o", -0.13), ("direct", "#C34E00", "s", 0.13))
    for j, (ax, (title, profile, schemes, labels, metric, xlabel)) in enumerate(zip(axes, columns)):
        panel_title(ax, "abc"[j], title)
        values = []
        for mode, color, marker, delta in marks:
            samples = [matrix[mode, profile, s]["summary"][metric] for s in schemes]
            values.extend(samples)
            ys = [i + delta for i in range(len(schemes))]
            ax.plot(samples, ys, color=color, lw=1.1, alpha=0.65, zorder=2)
            ax.scatter(samples, ys, color=color, marker=marker, s=34, zorder=3)
            for sample, y in zip(samples, ys):
                label = f"{sample:.1%}" if metric == "host_hit_fraction" else f"{sample:.1f}"
                ax.annotate(label, (sample, y), xytext=(5, 0), textcoords="offset points",
                            va="center", color=color, fontsize=8.4)
        ax.set_yticks(range(len(schemes)))
        ax.set_yticklabels(labels)
        ax.set_ylim(len(schemes) - 0.5, -0.5)
        ax.set_xlabel(xlabel)
        ax.grid(axis="x")
        ax.tick_params(axis="y", length=0, labelsize=9.2)
        if metric == "reads_per_request":
            ax.set_xscale("log")
            ax.set_xlim(min(values) * .72, max(values) * 2.6)
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.xaxis.set_major_formatter(ScalarFormatter())
            ax.xaxis.set_minor_formatter(NullFormatter())
        elif metric == "host_hit_fraction":
            ax.set_xlim(0, 1)
            ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
        else:
            ax.set_xlim(0, max(values) * 1.30)
    handles = [Line2D([], [], color=color, marker=marker, label=MODES[mode]) for mode, color, marker, _ in marks]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.028), frameon=False, ncol=2)
    save(fig, output, "mechanism-ablation")


def stage_values(cell):
    fields = ("disk_ns", "staging_ns", "control_ns", "roce_ns", "h2d_ns")
    return [mean(t["counters"].get(field, 0) / t["summary"]["requests"] / 1e6 for t in cell["trials"])
            for field in fields]


def stages(matrix, output):
    fig, axes = plt.subplots(2, 2, figsize=(10.1, 5.20), sharex="col")
    fig.subplots_adjust(left=0.128, right=0.98, top=0.83, bottom=0.21, hspace=0.62, wspace=0.34)
    fig.suptitle("Where the time goes: native vs. RoCE loopback", x=0.126, ha="left",
                 fontsize=15, fontweight="bold", y=0.975)
    labels = ("File read", "Local staging", "Control", "RC transfer + staging", "Ascend H2D")
    colors = ("#455A68", "#83A5AF", "#D6AB4B", "#0072B2", "#008463")
    for j, (mode, mode_label) in enumerate(MODES.items()):
        upper = max(sum(stage_values(matrix[mode, p, s])) for p in PROFILES for s in ("native", "roce"))
        for i, (profile, name) in enumerate(PROFILES.items()):
            ax = axes[i, j]
            panel_title(ax, "abcd"[i * 2 + j], f"{name} / {mode_label}")
            for y, scheme in enumerate(("native", "roce")):
                left = 0
                for value, color in zip(stage_values(matrix[mode, profile, scheme]), colors):
                    if value:
                        ax.barh(y, value, left=left, color=color, height=0.55, edgecolor="white", linewidth=0.45)
                    left += value
                ax.annotate(f"{left:.1f}", (left, y), xytext=(4, 0), textcoords="offset points",
                            va="center", fontsize=8.7)
            ax.set_yticks([0, 1])
            ax.set_yticklabels(["Native", "RoCE RC"])
            ax.set_ylim(1.53, -0.53)
            ax.set_xlim(0, upper * 1.20)
            ax.grid(axis="x")
            ax.tick_params(axis="y", length=0)
        axes[-1, j].set_xlabel("Measured stages (mean ms per request)")
    handles = [Line2D([], [], color=color, linewidth=6, label=label) for label, color in zip(labels, colors)]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.035), frameon=False, ncol=5,
               fontsize=9, columnspacing=1.25)
    save(fig, output, "roce-stages")


def render(matrix, output):
    output.mkdir(parents=True, exist_ok=True)
    for profile in PROFILES:
        performance(matrix, output, profile)
    ecdf(matrix, output)
    ablations(matrix, output)
    stages(matrix, output)
