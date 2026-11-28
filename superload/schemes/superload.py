"""Prioritize eligible short reloads in a bounded, page-first read/H2D pipeline."""

import ctypes as C
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from ..dataset.prefix import GEOMETRY
from ..hardware.device import anonymous_huge_bytes, numa_affinity, stage_buffer
from .local import LocalSource


PAGE = GEOMETRY.page_bytes
EVENT_COMPLETE = 1  # aclrtEventRecordedStatus


@dataclass(frozen=True)
class PipelineConfig:
    head: int
    body: int
    depth: int
    readers: int
    tail: int = 0
    async_streams: int = 0
    reserve_reader: bool = False
    prioritize_reads: bool = False
    huge_pages: bool = False


def pipeline_config(io, host_buffer, *, cpu=False):
    if io == "direct":
        return PipelineConfig(head=1, body=4, depth=2, readers=1, tail=16, prioritize_reads=True)
    if host_buffer == "pinned":
        return PipelineConfig(head=2, body=8, depth=3, readers=3, async_streams=2)
    return PipelineConfig(head=1, body=8, depth=2, readers=1, reserve_reader=True, huge_pages=not cpu)


@dataclass
class Request:
    role: str
    blocks: tuple
    address: int
    arrival: int
    sent: int = 0
    received: int = 0
    inflight: int = 0
    start: int = 0
    ready: int = 0
    counters: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class Chunk:
    request: Request
    source: LocalSource
    begin: int
    end: int

    @property
    def amount(self):
        return (self.end - self.begin) * PAGE

    @property
    def address(self):
        return self.request.address + self.begin * PAGE


class BlockingCopy:
    """Run synchronous copies on a worker so admissions continue while H2D runs."""

    def __init__(self, device, stack):
        settings = {}
        if hasattr(device, "lib"):
            context = C.c_void_p()
            device.lib.aclrtGetCurrentContext.argtypes = [C.POINTER(C.c_void_p)]
            device.lib.aclrtSetCurrentContext.argtypes = [C.c_void_p]
            device.call("aclrtGetCurrentContext", C.byref(context))
            settings = {"initializer": device.call, "initargs": ("aclrtSetCurrentContext", context)}
        self.executor = stack.enter_context(ThreadPoolExecutor(max_workers=1, **settings))
        self.active = {}

    @property
    def capacity(self):
        return 1 - len(self.active)

    @staticmethod
    def transfer(chunk):
        stats = chunk.source.upload(chunk.address, chunk.amount)
        return stats, time.perf_counter_ns()

    def submit(self, chunk):
        self.active[self.executor.submit(self.transfer, chunk)] = chunk

    def completed(self):
        for future, chunk in list(self.active.items()):
            if future.done():
                stats, end = future.result()
                del self.active[future]
                yield chunk, stats, end


class AsyncCopy:
    """Keep CANN-pinned buffers reserved until their stream events complete."""

    def __init__(self, device, stack, count):
        self.device = device
        lib = device.lib
        handle, size = C.c_void_p, C.c_size_t
        lib.aclrtCreateStream.argtypes = [C.POINTER(handle)]
        lib.aclrtSynchronizeStream.argtypes = [handle]
        lib.aclrtDestroyStream.argtypes = [handle]
        lib.aclrtCreateEvent.argtypes = [C.POINTER(handle)]
        lib.aclrtRecordEvent.argtypes = [handle, handle]
        lib.aclrtQueryEventStatus.argtypes = [handle, C.POINTER(C.c_int)]
        lib.aclrtDestroyEvent.argtypes = [handle]
        lib.aclrtMemcpyAsync.argtypes = [handle, size, handle, size, C.c_int, handle]
        self.available = deque()
        self.active = {}
        for _ in range(count):
            stream, event = handle(), handle()
            device.call("aclrtCreateStream", C.byref(stream))
            stack.callback(device.call, "aclrtDestroyStream", stream)
            device.call("aclrtCreateEvent", C.byref(event))
            stack.callback(device.call, "aclrtDestroyEvent", event)
            stack.callback(device.call, "aclrtSynchronizeStream", stream)
            self.available.append((stream, event))

    @property
    def capacity(self):
        return len(self.available)

    def submit(self, chunk):
        stream, event = self.available.popleft()
        start = time.perf_counter_ns()
        self.device.call("aclrtMemcpyAsync", chunk.address, chunk.amount,
                         chunk.source.stager.address, chunk.amount, 1, stream)
        self.device.call("aclrtRecordEvent", event, stream)
        self.active[stream.value] = stream, event, chunk, start

    def completed(self):
        for handle, (stream, event, chunk, start) in list(self.active.items()):
            status = C.c_int()
            self.device.call("aclrtQueryEventStatus", event, C.byref(status))
            if status.value == EVENT_COMPLETE:
                self.device.call("aclrtSynchronizeStream", stream)
                end = time.perf_counter_ns()
                del self.active[handle]
                self.available.append((stream, event))
                yield chunk, Counter(hbm_bytes=chunk.amount, h2d_ns=end - start), end


class ReloadPipeline:
    def __init__(self, stack, device, fd, page_count, config, numa_node):
        self.config = config
        self.numa_node = numa_node if config.huge_pages else None
        self.buffer_bytes = max(config.head, config.body, config.tail, 4) * PAGE
        self.slots = config.depth + max(1, config.async_streams)
        with numa_affinity(self.numa_node):
            self.sources = [
                LocalSource(fd, stage_buffer(stack, device, self.buffer_bytes, pinned=bool(config.async_streams),
                                            huge_pages=config.huge_pages),
                            device, GEOMETRY, "page-first", page_count)
                for _ in range(self.slots)
            ]
        self.reader = stack.enter_context(ThreadPoolExecutor(max_workers=config.readers))
        self.copy = (AsyncCopy(device, stack, config.async_streams) if config.async_streams
                     else BlockingCopy(device, stack))

    def memory(self):
        huge = (anonymous_huge_bytes([source.stager.address for source in self.sources])
                if self.config.huge_pages else 0)
        return {"host_pool_bytes": self.slots * self.buffer_bytes, "anon_huge_bytes": huge,
                "numa_node": self.numa_node}

    def run(self, pair, gap, address):
        with numa_affinity(self.numa_node):
            start = time.perf_counter_ns()
            low_blocks = tuple(sorted(pair["small"]))
            low = Request("small", low_blocks, address, start + gap * 1_000_000)
            high = Request("large", tuple(sorted(pair["large"])), address + len(low_blocks) * PAGE, start)
            self.schedule(low, high)
        rows = {}
        for request in (low, high):
            amount = len(request.blocks) * PAGE
            if request.counters["storage_bytes"] != amount or request.counters["hbm_bytes"] != amount:
                raise RuntimeError(f"wrong byte count for {request.role}")
            rows[request.role] = {
                "pages": len(request.blocks), "queue_ms": (request.start - request.arrival) / 1e6,
                "ready_ms": (request.ready - request.arrival) / 1e6,
                "service_ms": (request.ready - request.start) / 1e6, **request.counters,
            }
        return {"requests": rows, "elapsed_ms": (max(low.ready, high.ready) - start) / 1e6}, (low, high)

    def schedule(self, low, high):
        config = self.config
        free = deque(self.sources)
        reads = {}
        staged = []
        while not (low.ready and high.ready):
            for chunk, counts, end in self.copy.completed():
                request = chunk.request
                request.counters.update(counts)
                request.inflight -= 1
                request.received += chunk.end - chunk.begin
                if request.received == len(request.blocks):
                    request.ready = end
                free.append(chunk.source)
            for future, chunk in list(reads.items()):
                if future.done():
                    chunk.request.counters.update(future.result())
                    staged.append(chunk)
                    del reads[future]

            now = time.perf_counter_ns()
            while free and len(reads) < config.depth:
                if low.arrival <= now and low.sent < len(low.blocks):
                    request, blocks = low, 4
                else:
                    if high.sent == len(high.blocks) or high.inflight >= config.depth:
                        break
                    if config.reserve_reader and not low.ready and any(chunk.request is high for chunk in reads.values()):
                        break
                    if config.prioritize_reads and low.arrival <= now and any(
                        chunk.request is low for chunk in reads.values()
                    ):
                        break
                    request = high
                    blocks = config.head if not high.sent else (config.tail if low.ready and config.tail else config.body)
                end = min(len(request.blocks), request.sent + blocks)
                chunk = Chunk(request, free.popleft(), request.sent, end)
                request.sent = end
                request.inflight += 1
                if not request.start:
                    request.start = time.perf_counter_ns()
                reads[self.reader.submit(chunk.source.read, request.blocks[chunk.begin:chunk.end])] = chunk

            staged.sort(key=lambda chunk: chunk.request is high)
            while staged and self.copy.capacity:
                self.copy.submit(staged.pop(0))
            if low.ready and high.ready:
                return

            remaining = (low.arrival - time.perf_counter_ns()) / 1e9
            delay = remaining if remaining > 0 else None
            if self.copy.active:
                delay = min(delay, 0.00018) if delay is not None else 0.00018
            if reads:
                wait(reads, timeout=delay, return_when=FIRST_COMPLETED)
            elif delay is not None:
                time.sleep(delay)
            else:
                raise RuntimeError("reload stalled with incomplete requests")
