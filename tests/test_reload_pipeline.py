import unittest
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from superload.schemes import superload as pipeline


MS = 1_000_000
BASE = 100 * pipeline.PAGE


class Clock:
    def __init__(self):
        self.now = MS

    def perf_counter_ns(self):
        return self.now

    def sleep(self, seconds):
        self.now += round(seconds * 1_000_000_000)
        if self.now > 2_000 * MS:
            raise AssertionError("pipeline did not finish")

    def wait(self, reads, *, timeout, return_when):
        seconds = (min(read.end for read in reads) - self.now) / 1_000_000_000
        self.sleep(seconds if timeout is None else min(seconds, timeout))


class Buffer:
    pages = ()

    def read(self, pages):
        self.pages = pages
        return Counter(storage_bytes=len(pages) * pipeline.PAGE)


class Reader:
    def __init__(self, clock, small):
        self.clock = clock
        self.small = small
        self.pending_until = 0
        self.leased = {}
        self.admissions = []
        self.max_large_leased = 0

    def submit(self, read, pages):
        buffer = read.__self__
        if buffer in self.leased:
            raise AssertionError("read reused a buffer before H2D finished")
        role = "small" if pages[0] in self.small else "large"
        self.leased[buffer] = role
        self.max_large_leased = max(self.max_large_leased, list(self.leased.values()).count("large"))
        self.pending_until = max(self.pending_until, self.clock.now) + 5 * MS
        end = self.pending_until
        self.admissions.append((role, self.clock.now, end))
        return SimpleRead(end, self.clock, read, pages)


class SimpleRead:
    def __init__(self, end, clock, read, pages):
        self.end = end
        self.clock = clock
        self.read = read
        self.pages = pages

    def done(self):
        return self.clock.now >= self.end

    def result(self):
        return self.read(self.pages)


class DelayedCopy:
    def __init__(self, clock, leased):
        self.clock = clock
        self.leased = leased
        self.active = []
        self.submissions = []
        self.completions = []
        self.destination = {}

    @property
    def capacity(self):
        return 1 - len(self.active)

    def submit(self, chunk):
        self.submissions.append((chunk.request.role, chunk.end - chunk.begin, chunk.source))
        self.active.append((self.clock.now + 50 * MS, chunk))

    def completed(self):
        if self.active and self.clock.now >= self.active[0][0]:
            end, chunk = self.active.pop(0)
            for index, page in enumerate(chunk.source.pages):
                self.destination[chunk.address + index * pipeline.PAGE] = page
            del self.leased[chunk.source]
            yield chunk, Counter(hbm_bytes=chunk.amount), end
            self.completions.append((chunk.request.role, chunk.end, end, chunk.request.ready))


class ReloadPipelineTest(unittest.TestCase):
    def run_pair(self, small, gap, io="direct"):
        pair = {"small": small, "large": list(range(34, 9, -1))}
        clock = Clock()
        config = pipeline.pipeline_config(io, "pageable", cpu=True)
        reader = Reader(clock, pair["small"])
        copy = DelayedCopy(clock, reader.leased)
        with patch.multiple(pipeline, time=clock, wait=clock.wait, LocalSource=lambda *args: Buffer(),
                            stage_buffer=lambda *args, **kwargs: None,
                            ThreadPoolExecutor=lambda *, max_workers: reader,
                            BlockingCopy=lambda device, stack: copy):
            loader = pipeline.ReloadPipeline(SimpleNamespace(enter_context=lambda context: context),
                                             None, None, len(small) + len(pair["large"]), config, None)
            result, (low, high) = loader.run(pair, gap, BASE)
        self.assertFalse(reader.leased)
        expected_pages = sorted(pair["small"]) + sorted(pair["large"])
        self.assertEqual(copy.destination, {BASE + index * pipeline.PAGE: page
                                            for index, page in enumerate(expected_pages)})
        for request in (low, high):
            self.assertEqual(request.inflight, 0)
            size = len(request.blocks) * pipeline.PAGE
            self.assertEqual(result["requests"][request.role]["storage_bytes"], size)
            self.assertEqual(result["requests"][request.role]["hbm_bytes"], size)
        return reader, copy, (low, high), loader

    def test_simultaneous_ahead_of_high_and_ready_after_last_copy(self):
        reader, copy, (low, high), _ = self.run_pair([5, 2, 3, 0, 1], 0)
        self.assertEqual([role for role, _, _ in reader.admissions[:3]], ["small", "small", "large"])
        self.assertGreaterEqual(reader.admissions[2][1], reader.admissions[1][2])
        self.assertEqual([(role, count) for role, count, _ in copy.submissions[:2]], [("small", 4), ("small", 1)])
        low_completions = [entry for entry in copy.completions if entry[0] == "small"]
        self.assertEqual([entry[3] for entry in low_completions], [0, low_completions[-1][2]])
        self.assertLess(low.ready, high.ready)

    def test_staggered_admission_overtakes_staged_high_data_and_reuses_buffers(self):
        reader, copy, (low, high), loader = self.run_pair([3, 0], 25)
        self.assertEqual([(role, count) for role, count, _ in copy.submissions[:3]],
                         [("large", 1), ("small", 2), ("large", 4)])
        low_admission = next(entry for entry in reader.admissions if entry[0] == "small")
        self.assertEqual(low_admission[1], low.arrival)
        self.assertLess(low_admission[2], copy.completions[0][2])
        self.assertLess(low.ready, high.ready)
        self.assertEqual(reader.max_large_leased, loader.config.depth)
        self.assertIn(("large", loader.config.tail), [(role, count) for role, count, _ in copy.submissions])
        self.assertGreater(len(copy.submissions), loader.slots)

    def test_pageable_buffered_limits_high_reads_before_low_arrives(self):
        reader, copy, (low, high), _ = self.run_pair([3, 0], 25, io="buffered")
        first, second, third = reader.admissions[:3]
        self.assertEqual([first[0], second[0], third[0]], ["large", "large", "small"])
        self.assertEqual(second[1], first[2])
        self.assertEqual(third[1], low.arrival)
        self.assertEqual([role for role, _, _ in copy.submissions[:2]], ["large", "small"])
        self.assertLess(low.ready, high.ready)

    def test_pinned_streams_reclaim_only_the_completed_slot(self):
        clock = Clock()
        handles = iter(range(1, 5))
        recorded = []
        complete = set()
        synchronized = []

        def call(name, *args):
            if name in ("aclrtCreateStream", "aclrtCreateEvent"):
                args[0]._obj.value = next(handles)
            elif name == "aclrtRecordEvent":
                recorded.append(args[0].value)
            elif name == "aclrtQueryEventStatus":
                args[1]._obj.value = pipeline.EVENT_COMPLETE if args[0].value in complete else 0
            elif name == "aclrtSynchronizeStream":
                synchronized.append(args[0].value)

        device = SimpleNamespace(lib=Mock(), call=call)
        low = pipeline.Request("small", (0,), BASE, clock.now)
        high = pipeline.Request("large", (1,), BASE + pipeline.PAGE, clock.now)
        low_chunk = pipeline.Chunk(low, SimpleNamespace(stager=SimpleNamespace(address=10)), 0, 1)
        high_chunk = pipeline.Chunk(high, SimpleNamespace(stager=SimpleNamespace(address=20)), 0, 1)
        with patch.object(pipeline, "time", clock), ExitStack() as stack:
            copy = pipeline.AsyncCopy(device, stack, count=2)
            copy.submit(high_chunk)
            copy.submit(low_chunk)
            self.assertEqual(copy.capacity, 0)
            self.assertEqual(list(copy.completed()), [])
            self.assertEqual(synchronized, [])
            clock.sleep(0.001)
            complete.add(recorded[1])
            self.assertEqual(list(copy.completed()), [
                (low_chunk, Counter(hbm_bytes=pipeline.PAGE, h2d_ns=MS), clock.now),
            ])
            self.assertEqual(copy.capacity, 1)
            self.assertEqual(list(copy.completed()), [])
            complete.add(recorded[0])
            self.assertEqual([chunk for chunk, _, _ in copy.completed()], [high_chunk])
            self.assertEqual(copy.capacity, 2)
