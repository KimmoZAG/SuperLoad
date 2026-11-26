"""Entry point and scheme names for the KV replay benchmark."""

from .runner import run

SCHEMES = ("native", "roce", "strata", "bidaw", "solid")
CONTROLS = ("native-lru", "strata-io", "solid-io")

__all__ = ["run", "SCHEMES", "CONTROLS"]
