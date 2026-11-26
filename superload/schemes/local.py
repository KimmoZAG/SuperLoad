"""Local storage reads, DRAM staging, and SolidAttention prefetch."""

import time
from collections import Counter

from ..dataset.corpus import make_plan, page_extents, read_spans
from ..hardware.device import HostBuffer


class LocalSource:
    def __init__(self, fd, stager, device, spec, layout):
        self.fd = fd
        self.stager = stager
        self.device = device
        self.spec = spec
        self.layout = layout

    def load(self, pages, dest, cache=None, request=None, prefetched=None):
        length = len(pages) * self.spec.page_bytes
        metrics = Counter()
        start = time.perf_counter_ns()
        if cache is None and prefetched is None:
            spans = make_plan(self.layout, self.spec, pages)
        else:
            spans = []
            misses = []
            for i, page in enumerate(pages):
                target = i * self.spec.page_bytes
                cached = cache.get(page) if cache else prefetched.get(page)
                if cached is None:
                    spans.extend(page_extents(self.layout, self.spec, page, target))
                    misses.append((page, target))
                else:
                    self.stager.view[target:target + self.spec.page_bytes] = cached
                    key = "host_hit_pages" if cache else "prefetch_hit_pages"
                    metrics[key] += 1
        read_start = time.perf_counter_ns()
        calls, amount = read_spans(self.fd, self.stager.view, spans)
        read_done = time.perf_counter_ns()
        if cache and cache.capacity:
            for page, target in misses:
                data = bytes(self.stager.view[target:target + self.spec.page_bytes])
                owner = request.session if page >= 4 or request.selected else None
                cache.put(page, data, owner)
        copy_start = time.perf_counter_ns()
        self.device.upload(dest, self.stager.address, length)
        copy_done = time.perf_counter_ns()
        metrics.update(
            storage_bytes=amount, read_calls=calls, disk_ns=read_done - read_start,
            hbm_bytes=length, h2d_ns=copy_done - copy_start,
            staging_ns=(read_start - start) + (copy_start - read_done),
        )
        return metrics

    def prefetch(self, pages):
        start = time.perf_counter_ns()
        step = self.spec.page_bytes
        buffer = HostBuffer(len(pages) * step)
        try:
            calls, amount = read_spans(self.fd, buffer.view, make_plan(self.layout, self.spec, pages))
            values = {page: bytes(buffer.view[i * step:(i + 1) * step]) for i, page in enumerate(pages)}
        finally:
            buffer.close()
        return values, Counter(
            storage_bytes=amount, prefetch_bytes=amount, read_calls=calls, prefetch_calls=calls,
            prefetch_ns=time.perf_counter_ns() - start,
        )
