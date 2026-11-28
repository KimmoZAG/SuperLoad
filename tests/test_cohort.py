import hashlib
import json
import unittest

from experiments.cohort import select_cohort


class CohortTest(unittest.TestCase):
    def setUp(self):
        self.source = {
            "schema": 1, "trace_sha256": "trace", "trace_url": "trace.jsonl",
            "requests": 6, "source_windows": 3, "warmup_windows": 0, "page_bytes": 8,
            "file_page_hashes": [10, 11, 12, 13, 14, 15, 16, 17],
            "pairs": [
                {"timestamp_ms": 3000, "small": [4], "large": [7, 2]},
                {"timestamp_ms": 6000, "small": [1], "large": [3]},
                {"timestamp_ms": 9000, "small": [7], "large": [0, 6]},
            ],
        }
        payload = (json.dumps(self.source) + "\r\n").encode("utf-8")
        self.manifest = {
            "schema": 1,
            "source": {"plan_sha256": hashlib.sha256(payload).hexdigest()},
            "excluded": [{"id": "prior", "plan_sha256": "prior-digest", "timestamps_ms": [6000]}],
        }

    def test_keeps_remaining_windows_and_compacts_shared_page_ids(self):
        plan = select_cohort(self.source, self.manifest)
        self.assertEqual(plan["file_page_hashes"], [10, 12, 14, 16, 17])
        self.assertEqual(plan["pairs"], [
            {"timestamp_ms": 3000, "small": [2], "large": [4, 1]},
            {"timestamp_ms": 9000, "small": [4], "large": [0, 3]},
        ])
        self.assertEqual(plan["cohort"]["excluded_plan_sha256"], ["prior-digest"])

    def test_rejects_overlapping_or_unknown_exclusions(self):
        for timestamp, error in ((6000, "duplicate"), (12000, "outside the candidate plan")):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ValueError, error):
                select_cohort(self.source, {
                    **self.manifest,
                    "excluded": self.manifest["excluded"] + [
                        {"id": "other", "plan_sha256": "other-digest", "timestamps_ms": [timestamp]},
                    ],
                })
