"""AscendCL buffers and a CPU backend for tests of the data path."""

import ctypes as C
import mmap
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


class HostBuffer:
    def __init__(self, size, *, huge_pages=False):
        # Anonymous mmap supplies the alignment required by O_DIRECT.
        if huge_pages:
            alignment = 2 << 20
            self.mapping = mmap.mmap(-1, size + alignment, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            pointer = C.c_ubyte.from_buffer(self.mapping)
            offset = (-C.addressof(pointer)) % alignment
            del pointer
            try:
                self.mapping.madvise(mmap.MADV_HUGEPAGE, offset, size)
            except OSError:
                self.mapping.close()
                raise
        else:
            self.mapping = mmap.mmap(-1, size)
            offset = 0
        self.array = (C.c_ubyte * size).from_buffer(self.mapping, offset)
        self.address = C.addressof(self.array)
        self.view = memoryview(self.array).cast("B")

    def close(self):
        self.view.release()
        del self.array
        self.mapping.close()


def stage_buffer(stack, device, size, *, pinned=False, huge_pages=False):
    if pinned:
        alignment = os.sysconf("SC_PAGESIZE")
        raw = stack.enter_context(device.allocate_host(size + alignment))
        offset = (-raw.address) % alignment
        view = raw.view[offset:offset + size]
        stack.callback(view.release)
        slot = SimpleNamespace(address=raw.address + offset, view=view)
    else:
        slot = HostBuffer(size, huge_pages=huge_pages)
        stack.callback(slot.close)
    for offset in range(0, size, os.sysconf("SC_PAGESIZE")):
        slot.view[offset] = 0
    return slot


def anonymous_huge_bytes(addresses):
    total = 0
    selected = False
    with Path("/proc/self/smaps").open(encoding="ascii") as mappings:
        for line in mappings:
            if line.startswith("AnonHugePages:"):
                if selected:
                    total += int(line.split()[1]) * 1024
            elif fields := line.split(maxsplit=1):
                if "-" in fields[0]:
                    start, end = (int(value, 16) for value in fields[0].split("-"))
                    selected = any(start <= address < end for address in addresses)
    return total


@contextmanager
def numa_affinity(node):
    if node is None:
        yield
        return
    previous = os.sched_getaffinity(0)
    cores = set()
    path = Path(f"/sys/devices/system/node/node{node}/cpulist")
    for extent in path.read_text(encoding="ascii").strip().split(","):
        limits = extent.split("-")
        cores.update(range(int(limits[0]), int(limits[-1]) + 1))
    allowed = previous & cores
    if not allowed:
        raise ValueError(f"no allowed CPU cores on NUMA node {node}")
    os.sched_setaffinity(0, allowed)
    try:
        yield
    finally:
        os.sched_setaffinity(0, previous)


class Ascend:
    def __init__(self, device):
        self.lib = C.CDLL("libascendcl.so")
        self.device = device
        self.lib.aclInit.argtypes = [C.c_char_p]
        self.lib.aclFinalize.argtypes = []
        self.lib.aclrtSetDevice.argtypes = [C.c_int]
        self.lib.aclrtResetDevice.argtypes = [C.c_int]
        self.lib.aclrtMalloc.argtypes = [C.POINTER(C.c_void_p), C.c_size_t, C.c_int]
        self.lib.aclrtFree.argtypes = [C.c_void_p]
        self.lib.aclrtMallocHost.argtypes = [C.POINTER(C.c_void_p), C.c_size_t]
        self.lib.aclrtFreeHost.argtypes = [C.c_void_p]
        self.lib.aclrtMemcpy.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.c_int]
        self.lib.aclGetRecentErrMsg.restype = C.c_char_p

    def call(self, name, *args):
        status = getattr(self.lib, name)(*args)
        if status:
            detail = (self.lib.aclGetRecentErrMsg() or b"").decode(errors="replace")
            raise RuntimeError(f"{name} returned {status}: {detail}")

    def __enter__(self):
        self.call("aclInit", None)
        try:
            self.call("aclrtSetDevice", self.device)
        except BaseException:
            self.call("aclFinalize")
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.call("aclrtResetDevice", self.device)
        finally:
            self.call("aclFinalize")

    @contextmanager
    def allocate(self, size):
        ptr = C.c_void_p()
        self.call("aclrtMalloc", C.byref(ptr), size, 0)  # ACL_MEM_MALLOC_HUGE_FIRST
        try:
            yield ptr.value
        finally:
            self.call("aclrtFree", ptr)

    @contextmanager
    def allocate_host(self, size):
        ptr = C.c_void_p()
        self.call("aclrtMallocHost", C.byref(ptr), size)
        try:
            array = (C.c_ubyte * size).from_address(ptr.value)
            with memoryview(array).cast("B") as view:
                yield SimpleNamespace(address=ptr.value, view=view)
        finally:
            self.call("aclrtFreeHost", ptr)

    def upload(self, dest, source, size):
        self.call("aclrtMemcpy", dest, size, source, size, 1)

    def download(self, source, size):
        out = C.create_string_buffer(size)
        self.call("aclrtMemcpy", out, size, source, size, 2)
        return out.raw


class CPU:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    @contextmanager
    def allocate(self, size):
        buf = HostBuffer(size)
        try:
            yield buf.address
        finally:
            buf.close()

    @staticmethod
    def upload(dest, source, size):
        C.memmove(dest, source, size)

    @staticmethod
    def download(source, size):
        return C.string_at(source, size)
