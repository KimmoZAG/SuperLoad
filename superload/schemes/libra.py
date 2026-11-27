"""Adapt Libra's chunk pipeline to file read and H2D stages on one host."""

import time
from collections import Counter


class PipelinedSource:
    def __init__(self, reader, sources, chunk_blocks, block_bytes):
        self.reader = reader
        self.sources = sources
        self.chunk_blocks = chunk_blocks
        self.block_bytes = block_bytes

    @staticmethod
    def stage(source, pages):
        start = time.perf_counter_ns()
        counts = source.read(pages)
        return counts, start, time.perf_counter_ns()

    def load(self, pages, address):
        chunks = [pages[i:i + self.chunk_blocks] for i in range(0, len(pages), self.chunk_blocks)]
        counts = Counter()
        current = self.reader.submit(self.stage, self.sources[0], chunks[0])
        copy_start = copy_end = 0
        for index, chunk in enumerate(chunks):
            (read, start, end) = current.result()
            counts.update(read)
            counts["pipeline_overlap_ns"] += max(0, min(end, copy_end) - max(start, copy_start))
            slot = index % 2
            if index + 1 < len(chunks):
                current = self.reader.submit(self.stage, self.sources[1 - slot], chunks[index + 1])
            copy_start = time.perf_counter_ns()
            amount = len(chunk) * self.block_bytes
            counts.update(self.sources[slot].upload(address, amount))
            copy_end = time.perf_counter_ns()
            address += amount
        return counts
