"""ctypes bindings for the native libibverbs bridge."""

import ctypes as C
import os


class Endpoint:
    def __init__(self, args):
        self.args = args
        self.lib = C.CDLL(os.path.abspath(args.bridge))
        signatures = {
            "error": ([], C.c_char_p),
            "open": ([C.c_char_p, C.c_uint, C.c_int, C.c_uint], C.c_void_p),
            "address": ([C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32), C.c_void_p], None),
            "connect": ([C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p], C.c_int),
            "prepare_recv": ([C.c_void_p, C.c_size_t], C.c_int),
            "transfer": ([C.c_void_p, C.c_void_p, C.c_size_t, C.c_int, C.c_int], C.c_int),
            "close": ([C.c_void_p], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, "sl_roce_" + name)
            function.argtypes = arguments
            function.restype = result

    def __enter__(self):
        args = self.args
        self.handle = self.lib.sl_roce_open(args.hca.encode(), args.ib_port, args.gid_index, args.mr_bytes)
        if not self.handle:
            self.fail()
        return self

    def __exit__(self, *exc):
        self.lib.sl_roce_close(self.handle)

    def fail(self):
        raise RuntimeError(self.lib.sl_roce_error().decode(errors="replace"))

    def address(self):
        qpn, psn = C.c_uint32(), C.c_uint32()
        gid = C.create_string_buffer(16)
        self.lib.sl_roce_address(self.handle, C.byref(qpn), C.byref(psn), gid)
        return {"qpn": qpn.value, "psn": psn.value, "gid": gid.raw.hex()}

    def connect(self, peer):
        qpn, psn = peer["qpn"], peer["psn"]
        gid = bytes.fromhex(peer["gid"])
        if len(gid) != 16 or any(type(v) is not int or not 0 <= v < (1 << 24) for v in (qpn, psn)):
            raise ValueError("invalid RoCE QP address")
        if self.lib.sl_roce_connect(self.handle, qpn, psn, C.create_string_buffer(gid)):
            self.fail()

    def transfer(self, address, size, *, send):
        if self.lib.sl_roce_transfer(self.handle, address, size, send, self.args.timeout_ms):
            self.fail()

    def prepare_recv(self, size):
        if self.lib.sl_roce_prepare_recv(self.handle, size):
            self.fail()
