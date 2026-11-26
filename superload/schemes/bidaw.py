"""Trace-level policy components ported from the papers."""

from collections import OrderedDict, defaultdict, deque


class HostCache:
    def __init__(self, capacity, hint_aware=False):
        self.capacity = capacity
        self.hint_aware = hint_aware
        self.pages = OrderedDict()
        self.last_page = {}
        self.last_session = {}
        self.last_reply = {}
        self.distances = defaultdict(lambda: deque(maxlen=32))
        self.scores = {}
        self.sessions_in_wave = set()
        self.tick = 0

    def begin_request(self, req):
        if not self.hint_aware:
            return
        self.tick += 1
        prev = self.last_session.get(req.session)
        if prev is not None and req.session not in self.sessions_in_wave:
            distance = sum(last > prev for last in self.last_page.values())
            self.distances[req.session].append((self.last_reply[req.session], distance))
        self.sessions_in_wave.add(req.session)
        self.last_session[req.session] = self.tick
        for page in req.pages:
            self.last_page[page] = self.tick
        self.scores[req.session] = self.potential(req.session)

    def finish_wave(self, requests):
        if not self.hint_aware:
            return
        self.last_reply.update((req.session, req.answer_tokens) for req in requests)
        for session in self.sessions_in_wave:
            self.scores[session] = self.potential(session)
        self.sessions_in_wave.clear()

    def potential(self, session):
        answer = self.last_reply.get(session, 0)
        observations = self.distances[session]
        # Estimate a lower quantile from completed turns with similarly long replies.
        similar = sorted(d for a, d in observations if a >= answer)
        lower = similar[len(similar) // 10] if len(similar) >= 4 else 0
        weights = [2, 2, 1]
        for _, distance in observations:
            bucket = 0 if distance < self.capacity else 1 if distance < 2 * self.capacity else 2
            weights[bucket] += 1
        if lower >= self.capacity:
            weights[0] = 0
        if lower >= 2 * self.capacity:
            weights[1] = 0
        return (weights[0] + 0.35 * weights[1]) / sum(weights)

    def schedule(self, requests):
        def priority(req):
            misses = sum(page not in self.pages for page in req.pages)
            # Ready requests retain FIFO order. With simultaneous arrivals,
            # shorter missing-byte loads win the preparing-queue comparison.
            return misses

        return sorted(requests, key=priority) if self.hint_aware else requests

    def get(self, page):
        result = self.pages.get(page)
        if result is None:
            return None
        self.pages.move_to_end(page)
        return result[0]

    def put(self, page, payload, session):
        if not self.capacity:
            return
        self.pages[page] = (payload, session)
        if len(self.pages) > self.capacity:
            if self.hint_aware:
                victim = min(self.pages, key=lambda p: self.scores.get(self.pages[p][1], 1.0))
                self.pages.pop(victim)
            else:
                self.pages.popitem(last=False)
