"""HIXL READ into client HBM with a control channel for storage staging."""

import ctypes as C
import ipaddress
import json
import os
import secrets
import socket
import time
from contextlib import ExitStack
from dataclasses import asdict

from .corpus import make_plan, open_corpus, read_manifest, read_spans
from .device import Ascend, HostBuffer
from .workloads import PAGE_COUNT


class HixlError(RuntimeError):
    def __init__(self, phase, status):
        self.phase = phase
        self.status = status
        super().__init__(f"HIXL {phase} returned {status}")


class Endpoint:
    def __init__(self, bridge, local, comm_res):
        self.lib = C.CDLL(os.path.abspath(bridge))
        signatures = {
            "open": ([C.c_char_p, C.c_char_p, C.POINTER(C.c_uint)], C.c_void_p),
            "register": ([C.c_void_p, C.c_size_t, C.c_size_t, C.c_int], C.c_uint),
            "connect": ([C.c_void_p, C.c_char_p, C.c_int], C.c_uint),
            "read": ([C.c_void_p, C.c_size_t, C.c_size_t, C.c_size_t, C.c_int], C.c_uint),
            "close": ([C.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, "sl_hixl_" + name)
            function.argtypes = args
            function.restype = result
        self.local = local
        self.comm_res = comm_res or ""

    def __enter__(self):
        status = C.c_uint()
        self.handle = self.lib.sl_hixl_open(self.local.encode(), os.fsencode(self.comm_res), C.byref(status))
        if not self.handle:
            raise HixlError("Initialize", status.value)
        return self

    def __exit__(self, *exc):
        self.lib.sl_hixl_close(self.handle)

    def call(self, operation, phase, *args):
        status = getattr(self.lib, "sl_hixl_" + operation)(self.handle, *args)
        if status:
            raise HixlError(phase, status)

    def register(self, address, size, *, device):
        self.call("register", "RegisterMem", address, size, device)

    def connect(self, peer, timeout_ms):
        self.call("connect", "Connect", peer.encode(), timeout_ms)

    def read(self, dest, source, size, timeout_ms):
        self.call("read", "TransferSync", dest, source, size, timeout_ms)


def send(stream, record):
    stream.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
    stream.flush()


def receive(stream):
    line = stream.readline(8193)
    if not line:
        raise EOFError("HIXL control channel closed")
    if len(line) > 8192 or not line.endswith(b"\n"):
        raise ValueError("invalid HIXL control frame")
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("expected an HIXL control object")
    if "error" in record:
        raise RuntimeError(str(record["error"]))
    return record


def serve(args):
    spec, seed = read_manifest(args.corpus)
    length = args.max_pages * spec.page_bytes
    key = os.environ.get("SUPERLOAD_HIXL_KEY", "").encode()
    engine_host = args.engine.rsplit(":", 1)[0]
    if not key and not all(ipaddress.ip_address(host).is_loopback for host in (args.bind, engine_host)):
        raise ValueError("set SUPERLOAD_HIXL_KEY on both peers for non-loopback use")
    with ExitStack() as stack:
        acl = stack.enter_context(Ascend(args.device))
        pinned = stack.enter_context(acl.allocate(length, host=True))
        endpoint = stack.enter_context(Endpoint(args.bridge, args.engine, args.comm_res))
        endpoint.register(pinned, length, device=False)
        fd = open_corpus(args.corpus, "layer-major", spec, args.io)
        stack.callback(os.close, fd)
        window = memoryview((C.c_ubyte * length).from_address(pinned)).cast("B")
        stack.callback(window.release)
        bounce = None
        if args.io == "direct":
            bounce = HostBuffer(length)
            stack.callback(bounce.close)
        listener = stack.enter_context(socket.create_server((args.bind, args.control_port)))
        listener.settimeout(args.idle_timeout)
        print("HIXL server ready; source host memory registered", flush=True)
        client, _ = listener.accept()
        with client, client.makefile("rwb") as stream:
            client.settimeout(args.idle_timeout)
            hello = receive(stream)
            if not isinstance(hello.get("key"), str) or not secrets.compare_digest(hello["key"].encode(), key):
                send(stream, {"error": "invalid control credential"})
                return
            send(stream, {"schema": 1, "seed": seed, "geometry": asdict(spec),
                          "engine": args.engine, "max_pages": args.max_pages, "io": args.io})
            while True:
                try:
                    message = receive(stream)
                except EOFError:
                    break
                pages = message.get("pages")
                if (message.get("op") != "load" or not isinstance(pages, list) or
                        not 1 <= len(pages) <= args.max_pages or
                        any(type(page) is not int or not 0 <= page < PAGE_COUNT for page in pages)):
                    send(stream, {"error": "invalid page request"})
                    break
                start = time.perf_counter_ns()
                plan = make_plan("layer-major", spec, pages)
                calls, amount = read_spans(fd, bounce.view if bounce else window, plan)
                read_ns = time.perf_counter_ns() - start
                if bounce:
                    C.memmove(pinned, bounce.address, amount)
                send(stream, {"address": pinned, "bytes": amount, "calls": calls, "disk_ns": read_ns})
                if receive(stream).get("op") != "done":
                    break


class HixlSource:
    def __init__(self, args, spec, seed, dest, size):
        self.args = args
        self.spec = spec
        self.seed = seed
        self.dest = dest
        self.size = size

    def __enter__(self):
        args = self.args
        with ExitStack() as opening:
            control = opening.enter_context(socket.create_connection(
                (args.control_host, args.control_port), timeout=args.timeout_ms / 1000
            ))
            self.stream = opening.enter_context(control.makefile("rwb"))
            control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            send(self.stream, {"key": os.environ.get("SUPERLOAD_HIXL_KEY", "")})
            metadata = receive(self.stream)
            if (metadata["schema"], metadata["seed"], metadata["geometry"]) != (1, self.seed, asdict(self.spec)):
                raise ValueError("HIXL server corpus differs from the client manifest")
            if metadata["io"] != args.io:
                raise ValueError("HIXL server I/O mode differs from client configuration")
            self.max_pages = metadata["max_pages"]
            self.endpoint = opening.enter_context(Endpoint(args.bridge, args.local_engine, args.comm_res))
            self.endpoint.register(self.dest, self.size, device=True)
            self.endpoint.connect(metadata["engine"], args.timeout_ms)
            self.cleanup = opening.pop_all()
        return self

    def __exit__(self, *exc):
        # Disconnect while the server still holds its registered window; EOF releases it.
        self.cleanup.close()

    def load(self, pages, dest):
        if len(pages) > self.max_pages:
            raise ValueError("HIXL server staging window is too small")
        start = time.perf_counter_ns()
        send(self.stream, {"op": "load", "pages": list(pages)})
        ready = receive(self.stream)
        before_transfer = time.perf_counter_ns()
        if ready["bytes"] != len(pages) * self.spec.page_bytes:
            raise RuntimeError("HIXL staging length mismatch")
        self.endpoint.read(dest, ready["address"], ready["bytes"], self.args.timeout_ms)
        end = time.perf_counter_ns()
        send(self.stream, {"op": "done"})
        return {
            "storage_bytes": ready["bytes"], "read_calls": ready["calls"], "disk_ns": ready["disk_ns"],
            "hixl_ns": end - before_transfer, "hbm_bytes": ready["bytes"],
            "control_and_staging_ns": max(0, before_transfer - start - ready["disk_ns"]),
        }
