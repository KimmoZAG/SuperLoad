import unittest
from textwrap import dedent

from report.overview import render_overview, render_overview_parts, render_poster_results


class RenderOverviewTest(unittest.TestCase):
    def test_renders_completed_chapters_and_keeps_markdown(self):
        content = render_overview(dedent("""\
            # Overview

            ## Question

            - **KV** reload

            ## Empty

            ## Next chapter

            ```text
            ## Heading inside a code block
            ```
            """))

        self.assertEqual(content.count('class="overview-chapter"'), 2)
        self.assertIn("<h3>Question</h3>", content)
        self.assertIn("<strong>KV</strong>", content)
        self.assertIn("## Heading inside a code block", content)
        self.assertNotIn("Empty", content)
        self.assertNotIn("<h1>", content)

    def test_preserves_external_references_and_resolves_repo_links(self):
        content = render_overview(dedent("""\
            ## Motivation

            [Methods](docs/prefix-reload.md#复现) · [Paper](https://example.org/paper)

            ![Figure](https://example.org/chart.svg)
            """))

        self.assertIn('href="https://github.com/KimmoZAG/SuperLoad/blob/main/docs/prefix-reload.md#', content)
        self.assertIn('href="https://example.org/paper"', content)
        self.assertIn('src="https://example.org/chart.svg"', content)
        self.assertIn('alt="Figure"', content)

    def test_features_limitations_as_a_table_alongside_the_overview(self):
        source = dedent("""\
            ## What is the problem?（问题定义）

            KV reload.

            ## Why existing works fail?（现有方法的局限性）

            | 方法 | 重叠争用处理 |
            | --- | --- |
            | SuperLoad | **跨请求让行** |

            [实验说明](docs/comparison.md)
            """)

        overview, limitations = render_overview_parts(source)

        self.assertIn("KV reload.", overview)
        self.assertNotIn("跨请求让行", overview)
        self.assertIn('<div class="table-scroll limitations-table"', limitations)
        self.assertIn("<th>重叠争用处理</th>", limitations)
        self.assertIn("<td><strong>跨请求让行</strong></td>", limitations)
        self.assertIn('https://github.com/KimmoZAG/SuperLoad/blob/main/docs/comparison.md', limitations)

    def test_poster_uses_the_late_arrival_direct_measurement_for_both_bars(self):
        measurements = {
            "fifo": {
                "small_p95": {"median": 4000}, "large_p95": {"median": 3800}, "gib_s": {"median": 0.14},
            },
            "superload": {
                "small_p95": {"median": 500}, "large_p95": {"median": 4500}, "gib_s": {"median": 0.13},
            },
        }
        summary = {"measurements": {"direct": {"gaps": {25: measurements}, "pairs": 20, "repeat": 2}}}

        poster = render_poster_results(summary)

        self.assertIn("4.00 <small>s</small>", poster)
        self.assertIn("0.50 <small>s</small>", poster)
        self.assertIn("--bar-width:12.500%", poster)
        self.assertIn("3.80 → 4.50", poster)
        self.assertIn("0.140 → 0.130", poster)
        self.assertIn("20 对 × 2 轮", poster)
