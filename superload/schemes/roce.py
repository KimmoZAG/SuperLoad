"""Storage service and RoCE RC transport. TCP carries control frames only."""

import ipaddress
import json
import os
import secrets
import socket
import time
from contextlib import ExitStack
from dataclasses import asdict

from ..dataset.corpus import make_plan, open_corpus, read_manifest, read_spans
from ..dataset.workloads import PAGE_COUNT
from ..hardware.device import HostBuffer
from ..hardware.verbs import Endpoint


def send_frame(stream, record):
    stream.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
    stream.flush()


def read_frame(stream):
    line = stream.readline(8193)
    if not line:
        raise EOFError("RoCE control channel closed")
    if len(line) > 8192 or not line.endswith(b"\n"):
        raise ValueError("invalid RoCE control frame")
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("expected a control object")
    if "error" in record:
        raise RuntimeError(str(record["error"]))
    return record


def serve(args):
    spec, seed = read_manifest(args.corpus)
    key = os.environ.get("SUPERLOAD_ROCE_KEY", "").encode()
    if not key and not ipaddress.ip_address(args.bind).is_loopback:
        raise ValueError("set SUPERLOAD_ROCE_KEY on both endpoints for non-loopback use")
    with ExitStack() as stack:
        buffer = HostBuffer(args.max_pages * spec.page_bytes)
        stack.callback(buffer.close)
        fd = open_corpus(args.corpus, "layer-major", spec, args.io)
        stack.callback(os.close, fd)
        endpoint = stack.enter_context(Endpoint(args))
        listener = stack.enter_context(socket.create_server((args.bind, args.control_port)))
        listener.settimeout(args.idle_timeout)
        print(f"RoCE storage ready; control port {listener.getsockname()[1]}", flush=True)
        client, _ = listener.accept()
        with client, client.makefile("rwb") as stream:
            client.settimeout(args.idle_timeout)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = read_frame(stream)
            if not isinstance(hello.get("key"), str) or not secrets.compare_digest(hello["key"].encode(), key):
                send_frame(stream, {"error": "invalid control credential"})
                return
            if hello.get("mr_bytes") != args.mr_bytes:
                send_frame(stream, {"error": "RoCE registered window size differs on both peers"})
                return
            endpoint.connect(hello["address"])
            send_frame(stream, {"schema": 1, "seed": seed, "geometry": asdict(spec), "address": endpoint.address(),
                                "max_pages": args.max_pages, "io": args.io})
            while True:
                try:
                    message = read_frame(stream)
                except EOFError:
                    break
                pages = message.get("pages")
                if (not isinstance(pages, list) or not 1 <= len(pages) <= args.max_pages or
                        any(type(page) is not int or not 0 <= page < PAGE_COUNT for page in pages)):
                    send_frame(stream, {"error": "invalid page request"})
                    break
                plan = make_plan("layer-major", spec, pages)
                start = time.perf_counter_ns()
                calls, amount = read_spans(fd, buffer.view, plan)
                read_ns = time.perf_counter_ns() - start
                send_frame(stream, {"bytes": amount, "calls": calls, "disk_ns": read_ns})
                endpoint.transfer(buffer.address, amount, send=True)


class RoceSource:
    def __init__(self, args, spec, seed, device, max_pages):
        self.args = args
        self.spec = spec
        self.seed = seed
        self.device = device
        self.max_pages = max_pages

    def __enter__(self):
        args = self.args
        with ExitStack() as opening:
            self.buffer = HostBuffer(self.max_pages * self.spec.page_bytes)
            opening.callback(self.buffer.close)
            self.endpoint = opening.enter_context(Endpoint(args))
            control = opening.enter_context(socket.create_connection(
                (args.control_host, args.control_port), timeout=args.timeout_ms / 1000
            ))
            self.stream = opening.enter_context(control.makefile("rwb"))
            control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            send_frame(self.stream, {"key": os.environ.get("SUPERLOAD_ROCE_KEY", ""),
                                     "address": self.endpoint.address(), "mr_bytes": args.mr_bytes})
            metadata = read_frame(self.stream)
            if (metadata["schema"], metadata["seed"], metadata["geometry"]) != (1, self.seed, asdict(self.spec)):
                raise ValueError("RoCE storage corpus differs from the client manifest")
            if metadata["io"] != args.io:
                raise ValueError("RoCE storage I/O mode differs from the client configuration")
            self.server_pages = metadata["max_pages"]
            self.endpoint.connect(metadata["address"])
            self.cleanup = opening.pop_all()
        return self

    def __exit__(self, *exc):
        self.cleanup.close()

    def load(self, pages, dest):
        if len(pages) > self.server_pages:
            raise ValueError("RoCE storage staging window is too small")
        amount = len(pages) * self.spec.page_bytes
        start = time.perf_counter_ns()
        self.endpoint.prepare_recv(amount)
        send_frame(self.stream, {"pages": list(pages)})
        ready = read_frame(self.stream)
        transfer_start = time.perf_counter_ns()
        if ready["bytes"] != amount:
            raise RuntimeError("RoCE storage staging length mismatch")
        self.endpoint.transfer(self.buffer.address, amount, send=False)
        upload_start = time.perf_counter_ns()
        self.device.upload(dest, self.buffer.address, amount)
        end = time.perf_counter_ns()
        return {
            "storage_bytes": amount, "read_calls": ready["calls"], "disk_ns": ready["disk_ns"],
            "roce_bytes": amount, "roce_ns": upload_start - transfer_start,
            "hbm_bytes": amount, "h2d_ns": end - upload_start,
            "control_ns": max(0, transfer_start - start - ready["disk_ns"]),
        }
