import unittest
from types import SimpleNamespace

from scripts.mixed_kv.joint_schedule import Costs, Group, JointSchedule
from scripts.mixed_kv.trace import PREFIX, SPARSE


class JointScheduleTest(unittest.TestCase):
    def test_first_copy_stops_at_request_completion_boundary(self):
        trial = SimpleNamespace(policy="joint")
        scheduler = JointSchedule(trial, Costs(1, 0, 0, 1), 8, 1)
        old = SimpleNamespace(number=0, arrival=-10, finished=None)
        large = SimpleNamespace(number=1, arrival=0, finished=None)
        sparse = SimpleNamespace(number=2, arrival=0, finished=None)
        scheduler.groups = {
            PREFIX: Group(
                PREFIX,
                (old, large),
                tuple(range(8)),
                0,
                0,
                ((old, 2, 4), (large, 8, 16)),
                read_pages=8,
            ),
            SPARSE: Group(SPARSE, (sparse,), (192,), 8, 8, ((sparse, 1, 2),), read_pages=1),
        }
        read, copy = scheduler.plan(now=0)
        self.assertIsNone(read)
        self.assertEqual((copy.group, copy.start, copy.end), (PREFIX, 0, 2))
