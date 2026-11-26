"""Tie-rich inputs that a frozen quantization oracle can express (4.1 #6).

Only an official case whose generator takes explicit ``values`` (one per element of ``shape``)
can carry exact rounding ties: every other generator of such oracles passes its random input
through a tie repair that moves values off the .5 midpoints, so no seed can produce one.
For a symmetric per-channel quantizer (``mode`` 0: scale = absmax / qmax, rounded to float32,
code = round_half_even(x / scale)) this module builds values whose float32 quotient lands
exactly on k + 0.5. The arithmetic is IEEE float32, emulated with ``struct``: one float32
division computed in double and rounded once is correctly rounded (53 >= 2 * 24 + 2).

Nothing here changes the oracle; a variant only overrides the case's ``shape`` and ``values``.
"""
from __future__ import annotations

import hashlib
import math
import random
import struct
from typing import Any

# (rows, channels, share of elements that are exact ties): dense, moderate, and the sparse
# [128,128] class (ties in channels 0-3 only) the Quant bench author named as the judge's.
TIE_SHAPES = ((1025, 4, 0.5), (65, 128, 0.125), (128, 128, None))


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def quotient(x: float, scale: float) -> float:
    return f32(f32(x) / scale)


def explicit_case(cases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The official mode-0 quantization case whose generator takes one explicit value per element."""
    for case in cases:
        p = case.get("params") or {}
        shape, values = p.get("shape"), p.get("values")
        if (case.get("source") == "official" and isinstance(shape, list) and isinstance(values, list)
                and all(isinstance(n, int) for n in shape) and math.prod(shape) == len(values)
                and {"qmin", "qmax", "mode"} <= set(p) and int(p["mode"]) == 0):
            return case
    return None


def _scale(rng: random.Random, qmax: int, channel: int) -> tuple[float, float]:
    """(absmax, float32 scale) of one channel.

    Three channels in four use a power-of-two scale, so the division is exact and values print
    short; the fourth uses an arbitrary scale, where an inexact division can also break the tie.
    """
    if channel % 4 != 3:
        scale = 2.0 ** -rng.randint(2, 8)
        return f32(scale * qmax), scale
    absmax = f32(rng.uniform(0.5, 50.0))
    return absmax, f32(absmax / qmax)


def _tie(rng: random.Random, absmax: float, scale: float, qmax: int) -> float | None:
    for _ in range(64):
        k = rng.randint(-(qmax - 2), qmax - 2)
        target = k + 0.5
        x = f32(target * scale)
        for cand in (x, math.nextafter(x, math.inf), math.nextafter(x, -math.inf)):
            cand = f32(cand)
            if abs(cand) < absmax and quotient(cand, scale) == target:
                return cand
    return None


def _plain(rng: random.Random, absmax: float, scale: float) -> float:
    """A value well away from any rounding midpoint (dyadic sixteenths of the scale when exact)."""
    while True:
        if math.log2(scale).is_integer():
            x = scale * rng.randint(-int(absmax / scale) * 16 + 1, int(absmax / scale) * 16 - 1) / 16.0
        else:
            x = f32(rng.uniform(-absmax, absmax))
        r = quotient(x, scale)
        if abs((r - math.floor(r)) - 0.5) > 1e-3:
            return f32(x)


def build(seed_text: str, rows: int, chans: int, share: float | None, qmax: int) -> tuple[list[float], int]:
    """Row-major values of a [rows, chans] mode-0 input and the number of exact ties placed."""
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    grid = [[0.0] * chans for _ in range(rows)]
    ties = 0
    for c in range(chans):
        absmax, scale = _scale(rng, qmax, c)
        if f32(absmax / qmax) != scale:
            continue  # the oracle recomputes the scale from the absmax; only exact pairs are usable
        tie_rows = ({0} if c < 4 else set()) if share is None else set(rng.sample(range(1, rows), int(rows * share)))
        pin = rows - 1 if share is None else 0  # never a tie row
        for r in range(rows):
            value = _tie(rng, absmax, scale, qmax) if r in tie_rows and r != pin else None
            ties += value is not None
            grid[r][c] = value if value is not None else _plain(rng, absmax, scale)
        grid[pin][c] = absmax if rng.random() < 0.5 else -absmax  # pins the channel scale
    return [v for row in grid for v in row], ties


def tie_variants(base: dict[str, Any], seed_text: str) -> list[dict[str, Any]]:
    """Variant bodies (``base``, ``params``, ``name``) over TIE_SHAPES for an explicit-value case."""
    qmax = int(base["params"]["qmax"])
    out = []
    for rows, chans, share in TIE_SHAPES:
        values, ties = build(f"{seed_text}:{rows}x{chans}", rows, chans, share, qmax)
        density = "sparse ties in channels 0-3" if share is None else f"{share:.0%} exact ties"
        out.append({"base": base["id"], "params": {"shape": [rows, chans], "values": values},
                    "name": f"wa-repro: [{rows},{chans}] mode 0, {ties} exact .5 quotients ({density})"})
    return out
