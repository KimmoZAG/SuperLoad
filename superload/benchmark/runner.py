"""Open the resources needed for a measurement and run repeated trials."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from statistics import median

from ..dataset.corpus import open_corpus
from ..dataset.workloads import make_trace
from ..hardware.device import Ascend, CPU, HostBuffer
from ..schemes.local import LocalSource
from ..schemes.roce import RoceSource
from .replay import trial


def run(args, spec, seed):
    trace = make_trace(args.profile, args.seed, args.warmup + args.waves)
    max_pages = max(sum(len(req.pages) for req in wave.requests) for wave in trace)
    length = max_pages * spec.page_bytes
    layout = {
        "strata": "page-first", "strata-io": "page-first", "solid": "interleaved", "solid-io": "interleaved",
    }.get(args.scheme, "layer-major")
    use_roce = args.scheme == "roce"
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(length))
        pool = None
        if use_roce:
            request_pages = max(len(req.pages) for wave in trace for req in wave.requests)
            loader = stack.enter_context(RoceSource(args, spec, seed, device, request_pages))
        else:
            stager = HostBuffer(length)
            stack.callback(stager.close)
            fd = open_corpus(args.corpus, layout, spec, args.io)
            stack.callback(os.close, fd)
            loader = LocalSource(fd, stager, device, spec, layout)
            if args.scheme == "solid" and args.profile == "sparse-decode" and args.prefetch_pages:
                pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        trials = [trial(args, spec, seed, trace, device, base, loader, pool, layout) for _ in range(args.repeat)]
    medians = {key: median(t["summary"][key] for t in trials) for key in trials[0]["summary"]}
    return {"status": "ok", "layout": layout, "summary": medians, "trials": trials}
