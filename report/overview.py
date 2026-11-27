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
