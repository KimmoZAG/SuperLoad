import argparse
import tempfile
import unittest
from pathlib import Path

from superload.benchmark import CONTROLS, SCHEMES, run
from superload.dataset.corpus import generate
from superload.dataset.workloads import Geometry, PROFILES


class ReplayTest(unittest.TestCase):
    def test_two_workloads_and_all_local_paths_match_the_oracle(self):
        spec = Geometry(layers=2, heads=1, head_dim=8, tokens=4, dtype_bytes=2)
        with tempfile.TemporaryDirectory() as temp:
            generate(Path(temp), spec, seed=7)
            outputs = {}
            for profile in PROFILES:
                for scheme in (*SCHEMES, *CONTROLS):
                    if scheme == "roce":
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
