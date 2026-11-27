"""Read KV ranges into aligned DRAM and transfer them to HBM."""

import time
from collections import Counter

from ..dataset.corpus import make_plan, read_spans


class LocalSource:
    def __init__(self, fd, stager, device, spec, layout, page_count):
        self.fd = fd
        self.stager = stager
        self.device = device
        self.spec = spec
        self.layout = layout
        self.page_count = page_count

    def read(self, pages):
        start = time.perf_counter_ns()
        spans = make_plan(self.layout, self.spec, pages, self.page_count)
        read_start = time.perf_counter_ns()
        calls, amount = read_spans(self.fd, self.stager.view, spans)
        return Counter(storage_bytes=amount, read_calls=calls,
                       disk_ns=time.perf_counter_ns() - read_start, staging_ns=read_start - start)

    def upload(self, address, amount):
        start = time.perf_counter_ns()
        self.device.upload(address, self.stager.address, amount)
        return Counter(hbm_bytes=amount, h2d_ns=time.perf_counter_ns() - start)

    def load(self, pages, address):
        counts = self.read(pages)
        counts.update(self.upload(address, len(pages) * self.spec.page_bytes))
        return counts
