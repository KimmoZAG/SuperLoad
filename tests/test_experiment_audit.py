import random
import unittest
from itertools import product
from types import SimpleNamespace
from unittest.mock import patch

from experiments.audit import check_order, check_verification
from experiments.verify_hbm import verify_pair
from report.comparison_data import GAPS_MS, METHODS, PROFILES
from superload.benchmark.prefix import CORPUS_SEED
from superload.dataset.corpus import page_bytes


class ExperimentAuditTest(unittest.TestCase):
    def test_order_replays_pair_subset_and_detects_reordered_cases(self):
        seed = 37
        rng = random.Random(seed)
        pairs = sorted(rng.sample(range(6), 2))
        order = pairs.copy()
        rng.shuffle(order)
        trial = []
        for pair in order:
            cases = list(product(GAPS_MS, METHODS))
            rng.shuffle(cases)
            trial.extend({"pair": pair, "gap_ms": gap, "method": name, "host_buffer": "pageable"}
                         for gap, name in cases)
        run = {"pairs": pairs, "run_seed": seed, "host_buffers": ["pageable"], "trials": [trial]}

        check_order(run, "direct", 6)
        trial[0], trial[1] = trial[1], trial[0]
        with self.assertRaisesRegex(ValueError, "execution order mismatch"):
            check_order(run, "direct", 6)

    def test_full_check_requires_all_configurations_and_correct_page_count(self):
        plan = {"page_bytes": 16, "pairs": [{"small": [0], "large": [1, 2]}]}
        windows = [{"profile": profile, "pair": 0, "gap_ms": gap, "pages": 3,
                    "bytes": 48, "hbm_cleared": True} for profile, gap in product(PROFILES, GAPS_MS)]
        receipt = {"plan_sha256": "plan-hash", "windows": windows}

        self.assertEqual(check_verification(receipt, plan, "plan-hash", [0]), {"windows": 6, "pages": 18})
        windows[-1]["pages"] = 2
        with self.assertRaisesRegex(ValueError, "incomplete HBM verification"):
            check_verification(receipt, plan, "plan-hash", [0])

    def test_full_check_rejects_stale_hbm_contents_when_reload_skips_a_page(self):
        size = 16
        base = 64
        pair = {"small": [4], "large": [7]}
        payload = {base: page_bytes(CORPUS_SEED, 4, size), base + size: page_bytes(CORPUS_SEED, 7, size)}
        memory = dict(payload)

        class Device:
            def upload(self, address, source, count):
                memory[address] = bytes(count)

            def download(self, address, count):
                return memory[address]

        jobs = [SimpleNamespace(role="small", address=base, blocks=(4,)),
                SimpleNamespace(role="large", address=base + size, blocks=(7,))]

        def reload_both(*_):
            memory.update(payload)
            return {}, jobs

        def skip_small(*_):
            memory[base + size] = payload[base + size]
            return {}, jobs

        zero = SimpleNamespace(address=0)
        with patch("experiments.verify_hbm.GEOMETRY", SimpleNamespace(page_bytes=size)):
            self.assertEqual(verify_pair(Device(), SimpleNamespace(run=reload_both), zero, base, pair, 25), 2)
            with self.assertRaisesRegex(RuntimeError, "HBM data mismatch for small page 4"):
                verify_pair(Device(), SimpleNamespace(run=skip_small), zero, base, pair, 25)
