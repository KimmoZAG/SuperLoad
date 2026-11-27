"""KV tensor dimensions and physical block size."""


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
