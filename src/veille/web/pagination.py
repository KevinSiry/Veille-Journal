"""Tiny pagination helper."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    size: int
    total: int

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def count(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.count
