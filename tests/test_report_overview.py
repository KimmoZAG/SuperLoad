import unittest
from textwrap import dedent

from report.overview import render_overview


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
