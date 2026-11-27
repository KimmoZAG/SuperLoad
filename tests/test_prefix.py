import unittest

from superload.dataset.prefix import LARGE_MIN, replay


class PrefixReplayTest(unittest.TestCase):
    def test_cold_blocks_are_excluded_and_distinct_reloads_pair(self):
        large = tuple(range(10, 10 + LARGE_MIN))
        windows = [
            (0, [(1,), large]),
            (5, [(100,)]),
            (10, [(1, 200), (*large, 201)]),
        ]
        counts, sizes, candidates = replay(windows, (1, 1, 100), warmup=2, seed=0)
        self.assertEqual(counts["storage_requests"], 2)
        self.assertEqual(counts["mixed_windows"], 1)
        self.assertEqual(sizes, {1: 1, LARGE_MIN: 1})
        self.assertEqual(candidates, [(10, [((1,), large)])])

    def test_same_timestamp_cannot_admit_a_new_prefix_for_peer_request(self):
        counts, sizes, candidates = replay([(0, [(1,), (1,)])], (1, 2, 3), warmup=0, seed=0)
        self.assertEqual(counts["storage_requests"], 0)
        self.assertEqual(sizes, {0: 2})
        self.assertEqual(candidates, [])

    def test_shared_storage_blocks_do_not_form_a_disjoint_pair(self):
        large = tuple(range(1, LARGE_MIN + 1))
        windows = [(0, [large]), (10, [(100,)]), (20, [(1,), large])]
        counts, _, candidates = replay(windows, (1, 1, 100), warmup=2, seed=0)
        self.assertEqual(counts["mixed_windows"], 1)
        self.assertEqual(counts["disjoint_windows"], 0)
        self.assertEqual(candidates, [])
