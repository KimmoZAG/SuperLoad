import unittest

from superload.dataset.corpus import interleave, make_plan
from superload.dataset.workloads import Geometry, PAGE_COUNT, PROFILES, make_trace


class WorkloadsTest(unittest.TestCase):
    def test_trace_determinism_and_page_bounds(self):
        for profile in PROFILES:
            trace = make_trace(profile, 42, 32)
            self.assertEqual(trace, make_trace(profile, 42, 32))
            self.assertNotEqual(trace, make_trace(profile, 43, 32))
            for wave in trace:
                for request in wave.requests:
                    self.assertEqual(len(set(request.pages)), len(request.pages))
                    self.assertTrue(all(0 <= page < PAGE_COUNT for page in request.pages))
                    self.assertTrue(set(request.selected) <= set(request.pages))

    def test_page_first_coalesces_adjacent_pages(self):
        spec = Geometry()
        spans = make_plan("page-first", spec, (1, 2, 4))
        self.assertEqual([s.size for s in spans], [2 * spec.page_bytes, spec.page_bytes])

    def test_interleaving_preserves_k_and_v(self):
        spec = Geometry(layers=2, heads=1, head_dim=1, tokens=3, dtype_bytes=1)
        self.assertEqual(interleave(spec, b"abcABCdefDEF"), b"aAbBcCdDeEfF")
