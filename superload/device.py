"""AscendCL buffers and a CPU backend for tests of the data path."""

import ctypes as C
import mmap
from contextlib import contextmanager


class HostBuffer:
    def __init__(self, size):
        # Anonymous mmap supplies the alignment required by O_DIRECT.
        self.mapping = mmap.mmap(-1, size)
        self.array = (C.c_ubyte * size).from_buffer(self.mapping)
        self.address = C.addressof(self.array)
        self.view = memoryview(self.array).cast("B")

    def close(self):
        self.view.release()
        del self.array
        self.mapping.close()


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
