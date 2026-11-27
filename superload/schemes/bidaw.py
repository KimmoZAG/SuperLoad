"""Bidaw's Disk-HRRN ordering for requests that need storage reads."""


def disk_hrrn(ready, now):
    # The constant term and fixed bytes per block cancel when comparing priorities.
    return max(ready, key=lambda r: ((now - r.arrival) / len(r.blocks), -len(r.blocks)))
