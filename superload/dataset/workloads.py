"""Deterministic traces for prefix reloads and sparse decode."""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    layers: int = 4
    heads: int = 8
    head_dim: int = 128
    tokens: int = 128
    dtype_bytes: int = 2

    def __post_init__(self):
        if min(self.layers, self.heads, self.head_dim, self.tokens, self.dtype_bytes) <= 0:
            raise ValueError("KV dimensions must be positive")

    @property
    def vector_bytes(self):
        return self.heads * self.head_dim * self.dtype_bytes

    @property
    def plane_bytes(self):
        return self.tokens * self.vector_bytes

    @property
    def page_bytes(self):
        return 2 * self.layers * self.plane_bytes


@dataclass(frozen=True)
class Request:
    session: int
    pages: tuple[int, ...]
    selected: tuple[int, ...] = ()
    answer_tokens: int = 0


@dataclass(frozen=True)
class Wave:
    requests: tuple[Request, ...]
    gap_ms: float = 0.0


PROFILES = ("interactive-prefix", "sparse-decode")
PAGE_COUNT = 192


def interactive_prefix(seed, waves):
    rng = random.Random(seed)
    # Four shared system-prefix pages, followed by per-session history.
    sizes = [4 + rng.randrange(2, 13) for _ in range(12)]
    previous_sessions = []
    answers = {}
    for _ in range(waves):
        dominant = rng.sample(range(12), 2)
        if previous_sessions and rng.random() < 0.8:
            # Output length affects session return rate, not the actual lookup policy.
            dominant[0] = rng.choices(
                previous_sessions,
                weights=[1 / (1 + answers[s] / 96) for s in previous_sessions],
            )[0]
        sessions = dominant + rng.choices(range(12), k=2)
        rng.shuffle(sessions)
        entries = []
        for session in sessions:
            start = 4 + session * 12
            pages = (*range(4), *range(start, start + sizes[session] - 4))
            answer = rng.choice([24, 48, 96, 192, 384, 768])
            entries.append(Request(session, pages, answer_tokens=answer))
            answers[session] = answer
        yield Wave(tuple(entries))
        previous_sessions = sorted(set(sessions))


def sparse_decode(seed, waves):
    rng = random.Random(seed)
    # Stand-in scores with controlled query drift and occasional topic changes.
    scores = [[rng.random() for _ in range(96)] for _ in range(2)]
    focus = [rng.random(), rng.random()]
    for step in range(waves):
        session = (step // 12) % 2
        base = session * 96
        focus[session] = (
            rng.random() if step % 12 == 0 else (focus[session] + rng.uniform(-0.025, 0.025)) % 1
        )
        end = 80 + (step // 2) % 16
        chosen = sorted(
            range(2, end - 1),
            key=lambda i: abs(scores[session][i] - focus[session]) + rng.random() * 0.016,
        )[:6]
        selected = tuple(sorted(base + i for i in chosen))
        pages = (base, base + 1, *selected, base + end - 1, base + end)
        yield Wave((Request(session, pages, selected),), gap_ms=4.0)


def make_trace(profile, seed, waves):
    if waves < 1:
        raise ValueError("waves must be positive")
    generators = {"interactive-prefix": interactive_prefix, "sparse-decode": sparse_decode}
    return tuple(generators[profile](seed, waves))
