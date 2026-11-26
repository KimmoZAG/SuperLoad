import argparse
import tempfile
import unittest
from pathlib import Path

from superload.benchmark import CONTROLS, SCHEMES, run
from superload.corpus import generate, interleave, make_plan
from superload.workloads import Geometry, PAGE_COUNT, PROFILES, make_trace


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


class ReplayTest(unittest.TestCase):
    def test_two_workloads_and_all_local_paths_match_the_oracle(self):
        spec = Geometry(layers=2, heads=1, head_dim=8, tokens=4, dtype_bytes=2)
        with tempfile.TemporaryDirectory() as temp:
            generate(Path(temp), spec, seed=7)
            outputs = {}
            for profile in PROFILES:
                for scheme in (*SCHEMES, *CONTROLS):
                    if scheme == "hixl":
                        continue
                    options = argparse.Namespace(
                        corpus=temp, scheme=scheme, profile=profile, device="cpu",
                        io="buffered", seed=42, cache_pages=24, prefetch_pages=6, warmup=1,
                        waves=8, repeat=1, verify="all",
                    )
                    outputs[profile, scheme] = run(options, spec, 7)["trials"][0]["counters"]
            dense = "interactive-prefix"
            sparse = "sparse-decode"
            self.assertLess(outputs[dense, "strata"]["hbm_bytes"], outputs[dense, "native"]["hbm_bytes"])
            self.assertLess(outputs[dense, "strata-io"]["read_calls"], outputs[dense, "native"]["read_calls"])
            self.assertGreater(outputs[dense, "bidaw"]["host_hit_pages"], 0)
            self.assertGreater(outputs[sparse, "solid"]["prefetch_bytes"], 0)
            self.assertGreater(outputs[sparse, "solid"]["prefetch_hit_pages"], 0)


if __name__ == "__main__":
    unittest.main()
