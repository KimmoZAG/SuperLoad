import unittest

from superload.dataset.workloads import Request
from superload.schemes.bidaw import HostCache


class BidawTest(unittest.TestCase):
    def test_bidaw_only_uses_answers_from_completed_waves(self):
        cache = HostCache(2, hint_aware=True)
        first = Request(1, (1, 2), answer_tokens=24)
        cache.begin_request(first)
        self.assertNotIn(1, cache.last_reply)
        cache.finish_wave((first,))
        later = Request(1, (1, 2), answer_tokens=768)
        cache.begin_request(later)
        cache.begin_request(later)
        self.assertEqual(cache.last_reply[1], 24)
        self.assertEqual(tuple(cache.distances[1]), ((24, 0),))
        cache.finish_wave((later, later))
        self.assertEqual(cache.last_reply[1], 768)
