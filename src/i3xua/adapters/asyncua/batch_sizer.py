"""Adaptive batch sizing for Variable-metadata Reads.

Port of project-barracuda ``opcua/client/batch_sizer.go`` and
``readMetadataAdaptive`` (browse_recursive.go). Servers enforce a per-Read
operation cap that they may not publish (the .NET Reference Server rejects
large metadata Reads with ``BadTooManyOperations``), and very large Reads
against tag-heavy servers (Kepware) take long enough to hit the client
request timeout. The sizer keeps each Read inside both limits:

- slow-start: the window doubles on each success until ``ssthresh``;
- AIMD above ``ssthresh``: +``aimd_increment`` on success, halve on rejection;
- a rejected size becomes a sticky hard cap (``rejected - 1``) for the session;
- a measured per-item latency caps the window to ``target_latency_ms``;
- the declared ceiling (``MaxNodesPerRead`` / 7) always wins over the floor.

Sizes count *logical units* (Variables), not ReadValueIds; each unit is
``alignment`` contiguous ReadValueIds, and splits always land on a unit
boundary.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from asyncua.ua.uaerrors import BadTooManyOperations

DEFAULT_FLOOR = 100
DEFAULT_CEILING = 200_000
DEFAULT_SLOW_START_SEED = 100
DEFAULT_SSTHRESH_SEED = 10_000
DEFAULT_AIMD_INCREMENT = 500
DEFAULT_EWA_ALPHA = 0.2  # weight of the newest msPerItem sample
DEFAULT_TARGET_LATENCY_MS = 2000.0

# ReadValueIds per metadata Read when the server does not publish
# OperationLimits.MaxNodesPerRead. 175K ≈ 25K Variables at 7 attrs each;
# measured sweet spot against a 770K-tag Kepware in barracuda.
DEFAULT_READ_CHUNK_VALUE_IDS = 175_000


@dataclass(frozen=True)
class BatchSizerSnapshot:
    window: int
    ssthresh: int
    hard_cap: int
    floor: int
    ceiling: int
    ms_per_item: float


class BatchSizer:
    """Per-session adaptive window. Not thread-safe; one per browse source
    (the asyncua loop is single-threaded and metadata Reads are sequential)."""

    def __init__(
        self,
        *,
        ceiling: int = 0,
        floor: int = 0,
        seed_ssthresh: int = 0,
        seed_window: int = 0,
        aimd_increment: int = 0,
        target_latency_ms: float = 0.0,
    ) -> None:
        ceiling = ceiling if ceiling > 0 else DEFAULT_CEILING
        floor = floor if floor > 0 else DEFAULT_FLOOR
        seed_ssthresh = seed_ssthresh if seed_ssthresh > 0 else DEFAULT_SSTHRESH_SEED
        seed_window = seed_window if seed_window > 0 else DEFAULT_SLOW_START_SEED
        aimd_increment = aimd_increment if aimd_increment > 0 else DEFAULT_AIMD_INCREMENT
        if target_latency_ms <= 0 or math.isnan(target_latency_ms) or math.isinf(target_latency_ms):
            target_latency_ms = DEFAULT_TARGET_LATENCY_MS
        # The ceiling is the server's declared limit; the floor is only our
        # anti-starvation guard, so the ceiling wins when they conflict.
        floor = min(floor, ceiling)
        self.floor = floor
        self.ceiling = ceiling
        self.ssthresh = min(seed_ssthresh, ceiling)
        self.window = min(seed_window, ceiling)
        self.hard_cap = 0  # 0 = unknown
        self.aimd_increment = aimd_increment
        self.target_latency_ms = target_latency_ms
        self.ms_per_item = 0.0

    def next(self) -> int:
        sz = min(self.window, self.ceiling)
        if self.hard_cap > 0:
            sz = min(sz, self.hard_cap)
        if self.ms_per_item > 0:
            sz = min(sz, int(self.target_latency_ms / self.ms_per_item))
        return max(sz, self.floor)

    def on_success(self, n: int, elapsed_s: float) -> None:
        if n > 0:
            mspi = elapsed_s * 1000.0 / n
            if self.ms_per_item == 0:
                self.ms_per_item = mspi
            else:
                self.ms_per_item = (
                    1 - DEFAULT_EWA_ALPHA
                ) * self.ms_per_item + DEFAULT_EWA_ALPHA * mspi
        if self.window < self.ssthresh:
            self.window *= 2
        else:
            self.window += self.aimd_increment
        self.window = min(self.window, self.ceiling)
        if self.hard_cap > 0:
            self.window = min(self.window, self.hard_cap)

    def on_rejection(self, rejected_size: int) -> None:
        """``BadTooManyOperations`` at ``rejected_size`` units."""
        if rejected_size <= self.floor:
            return  # cannot shrink further; caller surfaces the failure
        new_cap = rejected_size - 1
        if self.hard_cap == 0 or new_cap < self.hard_cap:
            self.hard_cap = new_cap
        self.ssthresh = max(min(rejected_size // 2, self.hard_cap), self.floor)
        self.window = self.ssthresh

    def snapshot(self) -> BatchSizerSnapshot:
        return BatchSizerSnapshot(
            window=self.window,
            ssthresh=self.ssthresh,
            hard_cap=self.hard_cap,
            floor=self.floor,
            ceiling=self.ceiling,
            ms_per_item=self.ms_per_item,
        )


@dataclass
class MetadataReadFeedback:
    """Rejected sizes and accepted sub-Reads, in logical units."""

    splits: int = 0
    rejected_sizes: list[int] = field(default_factory=list)
    accepted: list[tuple[int, float]] = field(default_factory=list)  # (units, elapsed_s)


ReadFn = Callable[[Sequence[Any]], Awaitable[list[Any]]]


async def read_metadata_adaptive(
    read: ReadFn, items: Sequence[Any], alignment: int
) -> tuple[list[Any], MetadataReadFeedback]:
    """Read ``items``; on ``BadTooManyOperations`` split in half on an
    ``alignment`` boundary and recurse. Any other error propagates (a smaller
    Read won't help a transport or timeout failure).

    Raises ``BadTooManyOperations`` if a single unit is still rejected. On
    any raise, ``exc.feedback`` carries the rejected sizes observed so far.
    """
    if alignment <= 0 or len(items) % alignment != 0:
        raise ValueError(f"metadata Read: {len(items)} items not aligned to {alignment}")
    fb = MetadataReadFeedback()
    units = len(items) // alignment
    t0 = time.monotonic()
    try:
        results = await read(items)
    except BadTooManyOperations as exc:
        fb.rejected_sizes.append(units)
        if units <= 1:
            setattr(exc, "feedback", fb)  # noqa: B010
            raise
    except Exception as exc:
        setattr(exc, "feedback", fb)  # noqa: B010
        raise
    else:
        fb.accepted.append((units, time.monotonic() - t0))
        return list(results), fb

    mid_units = max(units // 2, 1)
    mid = mid_units * alignment
    out: list[Any] = []
    fb.splits = 1
    for part in (items[:mid], items[mid:]):
        try:
            res, sub = await read_metadata_adaptive(read, part, alignment)
        except Exception as exc:
            sub = getattr(exc, "feedback", MetadataReadFeedback())
            fb.splits += sub.splits
            fb.rejected_sizes.extend(sub.rejected_sizes)
            setattr(exc, "feedback", fb)  # noqa: B010
            raise
        fb.splits += sub.splits
        fb.rejected_sizes.extend(sub.rejected_sizes)
        fb.accepted.extend(sub.accepted)
        out.extend(res)
    return out, fb
