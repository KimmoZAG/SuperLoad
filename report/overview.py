"""Render the completed research overview chapters for Pages."""

from urllib.parse import urljoin

from markdown_it import MarkdownIt


GITHUB_OVERVIEW = "https://github.com/KimmoZAG/SuperLoad/blob/main/overview.md"
RAW_OVERVIEW = "https://raw.githubusercontent.com/KimmoZAG/SuperLoad/main/overview.md"


def render_overview(source):
    markdown = MarkdownIt("js-default")
    tokens = markdown.parse(source)
    headings = [
        index for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.tag == "h2" and token.level == 0
    ]
    chapters = []
    for start, end in zip(headings, headings[1:] + [len(tokens)]):
        chapter = tokens[start:end]
        if len(chapter) == 3:
            continue

        for token in chapter:
            if token.type in ("heading_open", "heading_close"):
                token.tag = f"h{min(int(token.tag[1:]) + 1, 6)}"
            for inline in token.children or ():
                if inline.type == "link_open":
                    inline.attrSet("href", urljoin(GITHUB_OVERVIEW, inline.attrGet("href")))
                elif inline.type == "image":
                    inline.attrSet("src", urljoin(RAW_OVERVIEW, inline.attrGet("src")))
                    inline.attrSet("loading", "lazy")
                    inline.attrSet("decoding", "async")
        body = markdown.renderer.render(chapter, markdown.options, {})
        chapters.append(f'<article class="overview-chapter">\n{body}</article>')
    return "\n".join(chapters)


def render_poster_results(summary):
    direct = summary["measurements"]["direct"]
    cases = direct["gaps"][25]
    fifo = cases["fifo"]
    superload = cases["chunk-rr"]
    longest = max(row["small_p95"]["median"] for row in (fifo, superload))
    small_rows = []
    for style, title, values in (("fifo", "原生 FIFO", fifo), ("superload", "SuperLoad", superload)):
        latency = values["small_p95"]["median"]
        small_rows.append(
            f'<li class="result-bar result-bar-{style}">'
            f'<span>{title}</span><strong>{latency / 1000:.2f} <small>s</small></strong>'
            f'<span class="result-track" aria-hidden="true">'
            f'<span style="--bar-width:{100 * latency / longest:.3f}%"></span></span></li>'
        )
    large_before = fifo["large_p95"]["median"] / 1000
    large_after = superload["large_p95"]["median"] / 1000
    throughput_before = fifo["gib_s"]["median"]
    throughput_after = superload["gib_s"]["median"]
    return (
        '<section class="poster-results" aria-labelledby="poster-results-title">'
        '<h2 id="poster-results-title">低回载量请求 P95</h2>'
        f'<ul class="result-bars">{"".join(small_rows)}</ul>'
        '<dl class="poster-tradeoffs">'
        f'<div><dt>高回载量请求 P95</dt><dd>{large_before:.2f} → {large_after:.2f} <small>s</small></dd></div>'
        f'<div><dt>双请求吞吐</dt><dd>{throughput_before:.3f} → {throughput_after:.3f} '
        '<small>GiB/s</small></dd></div></dl>'
        '<p class="result-context">Direct I/O · 高回载量请求早到 25 ms · '
        f'{direct["pairs"]} 对 × {direct["repeat"]} 轮</p>'
        '<a class="result-link" href="#comparison">查看实测图 <span aria-hidden="true">↓</span></a></section>'
    )
