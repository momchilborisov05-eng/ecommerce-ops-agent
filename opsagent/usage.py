"""Token usage and cost per run."""

from __future__ import annotations

from dataclasses import dataclass

# USD per million tokens: (input, output, cache read, cache write 5 min).
# Cache writes cost 1.25x input. Unknown models are priced as Opus 5.5. Update when prices change.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}


@dataclass
class Usage:
    input_tokens: int = 0         # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self, model: str) -> float:
        p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
        return round(
            (self.input_tokens * p_in + self.output_tokens * p_out
             + self.cache_read_tokens * p_read + self.cache_write_tokens * p_write) / 1_000_000,
            6,
        )
