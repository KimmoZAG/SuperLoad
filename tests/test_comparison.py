import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from superload.benchmark.comparison import execute
from superload.dataset.corpus import make_plan
from superload.dataset.prefix import GEOMETRY
from superload.schemes.libra import PipelinedSource


class ComparisonTest(unittest.TestCase):
    def run_pair(self, method, gap):
        pair = {"small": [0], "large": list(range(1, 17))}
        calls = []
        now = 0

        class Loader:
            def load(self, pages, address):
                nonlocal now
                calls.append((pages, address))
                now += len(pages) * 10_000_000
                size = len(pages) * GEOMETRY.page_bytes
                return {"storage_bytes": size, "hbm_bytes": size}

        with patch("superload.benchmark.comparison.time.perf_counter_ns", side_effect=lambda: now):
            result, _ = execute(pair, gap, method, Loader(), 0, chunk_blocks=2)
        return result["requests"], calls

    def test_bidaw_orders_pending_requests_but_cannot_interrupt_one(self):
        simultaneous, calls = self.run_pair("bidaw", 0)
        self.assertEqual(calls[0][0], (0,))
        self.assertEqual(simultaneous["small"]["ready_ms"], 10)
        staggered, calls = self.run_pair("bidaw", 25)
        self.assertEqual(len(calls[0][0]), 16)
        self.assertEqual(staggered["small"]["ready_ms"], 145)

    def test_round_robin_admits_late_job_at_chunk_boundary(self):
        rows, calls = self.run_pair("chunk-rr", 25)
        self.assertEqual([c[0] for c in calls[:3]], [(1, 2), (3, 4), (0,)])
        self.assertEqual(rows["small"]["ready_ms"], 25)
        self.assertEqual(rows["large"]["ready_ms"], 170)

    def test_page_first_coalesces_consecutive_blocks(self):
        spans = make_plan("page-first", GEOMETRY, [1, 2, 5], 10)
        self.assertEqual([span.size for span in spans], [2 * GEOMETRY.page_bytes, GEOMETRY.page_bytes])

    def test_libra_preserves_all_chunks_across_reused_buffers(self):
        destination = {}

        class Buffer:
            def read(self, pages):
                self.bytes = bytes(pages)
                return Counter(storage_bytes=len(pages))

            def upload(self, address, amount):
                if amount != len(self.bytes):
                    raise ValueError("staged chunk has the wrong size")
                destination.update(enumerate(self.bytes, address))
                return Counter(hbm_bytes=amount)

        pages = [19, 3, 35, 7, 48, 5, 2]
        with ThreadPoolExecutor(max_workers=1) as reader:
            source = PipelinedSource(reader, (Buffer(), Buffer()), chunk_blocks=2, block_bytes=1)
            counts = source.load(pages, 100)
        self.assertEqual(destination, dict(enumerate(pages, 100)))
        self.assertEqual(counts["storage_bytes"], counts["hbm_bytes"])
