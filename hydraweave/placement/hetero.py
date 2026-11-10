"""Heterogeneous DS decode placement across the RTX PRO and the Strix pair: candidate plans and predicted tok/s.

Every unit gets a cost on each device and the plan chooses where it lives, minimising link crossings
per token. This module compares the plan families the engines can run today, each priced by a time model fitted on
the measured points of the heterogeneous-decode runs (October 2026, 1 user, R1, greedy, exact, 1 GbE):

- pair only (P15): 27.2 tok/s, every unit on the pair (measured);
- pipeline split: RTX layers [0, K) with every expert resident, pair [K, 40), one crossing per token;
  ms/token = a + b K, least squares on the five measured K = 8 / 12 / 16 / 20 / 22 (28.2 / 30.7 / 33.9 / 36.4 /
  38.4 tok/s); K outside 8..22 is marked extrapolated;
- dense + hot experts: attention, dense, shared expert and head of all 40 layers on the RTX (9.55 GiB reserve, W1),
  the hottest experts resident, the rest computed by the pair's expert servers; ms/token = 21 + 0.37 x remote
  layers/token (fit on static 24.2 remote -> 33.6 tok/s and dynamic 11.0 -> 40.1); remote layers/token =
  40 x (1 - layer_all_local), from the held-out hit-rate curve of the routes (the bundled ds41-hitrate.csv, replica mode);
  remote layers outside the measured 11.0..24.2 are marked extrapolated.

Every candidate says whether it is a measured point or a model output (`estimate`). The pair's weights never move
(Strix weights come only from local NVMe): only activations cross the link.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from ..vramplan import PlanError

LAYERS = 40
PAIR_ONLY_TOK_S = 27.2
PIPE_MEASURED = ((8, 28.2), (12, 30.7), (16, 33.9), (20, 36.4), (22, 38.4))  # (K, tok/s)
HOT = {"base_ms": 21.0, "per_remote_layer_ms": 0.37, "reserve_gib": 9.55, "measured_remote": (11.0, 24.2)}
CROSSING_SAVING_25G_MS = 0.42  # a 2.5 GbE switch, per pipeline crossing (an assumed saving; not measured)
LINKS = ("1g", "2.5g")


@dataclass
class Candidate:
    family: str
    flags: dict
    ms_per_token: float
    calibration: str
    detail: dict
    estimate: bool = True
    extrapolated: bool = False
    measured_point: dict | None = field(default=None)

    @property
    def tok_s(self):
        return 1000 / self.ms_per_token

    def to_dict(self):
        return {"family": self.family, "flags": self.flags, "ms_per_token": round(self.ms_per_token, 2),
                "tok_s": round(self.tok_s, 1), "estimate": self.estimate, "extrapolated": self.extrapolated,
                "measured_point": self.measured_point, "calibration": self.calibration, "detail": self.detail}


def _pipe_fit():
    """Least squares ms/token = a + b K over the measured pipeline points."""
    ks = [k for k, _ in PIPE_MEASURED]
    ms = [1000 / t for _, t in PIPE_MEASURED]
    mk, mm = sum(ks) / len(ks), sum(ms) / len(ms)
    b = sum((k - mk) * (m - mm) for k, m in zip(ks, ms)) / sum((k - mk) ** 2 for k in ks)
    return mm - b * mk, b


def pipeline(k, link="1g"):
    if type(k) is not int or not 1 <= k < LAYERS:
        raise PlanError(f"pipeline split: K must be an integer in 1..{LAYERS - 1}")
    if link not in LINKS:
        raise PlanError(f"pipeline split: link must be one of {LINKS}")
    a, b = _pipe_fit()
    ms = a + b * k - (CROSSING_SAVING_25G_MS if link == "2.5g" else 0.0)
    measured = dict(PIPE_MEASURED).get(k)
    ks = [x for x, _ in PIPE_MEASURED]
    return Candidate("pipeline", {"--cuts": k, "--layers": k, "--tiers": "D" * k, "tail --first-layer": k}, ms,
                     f"measured K=8/12/16/20/22 (least squares ms = {a:.2f} {b:+.3f} K)"
                     + ("; minus 0.42 ms for 2.5 GbE (not measured)" if link == "2.5g" else ""),
                     {"rtx_layers": k, "pair_layers": LAYERS - k, "crossings_per_token": 1, "link": link},
                     extrapolated=not min(ks) <= k <= max(ks) or link != "1g",
                     measured_point={"tok_s": measured, "link": "1g"} if measured else None)


def _interp(curve, x):
    pts = sorted(curve)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _check_curve(curve):
    if not curve or any(len(p) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in p)
                        or not 0 <= p[1] <= 1 for p in curve):
        raise PlanError("hit-rate curve: expected (expert GiB, layer_all_local in 0..1) finite points")


def hot(expert_gib, curve):
    """Dense + hot experts with `expert_gib` of statically resident experts; curve = [(expert GiB, layer_all_local)]
    held out from the routes (replica mode)."""
    _check_curve(curve)
    if not isinstance(expert_gib, (int, float)) or not math.isfinite(expert_gib) or expert_gib < 0:
        raise PlanError("hot placement: expert GiB must be a finite number >= 0 (no room after the 9.55 GiB reserve?)")
    local = _interp(curve, expert_gib)
    remote = LAYERS * (1 - local)
    low, high = HOT["measured_remote"]
    ms = HOT["base_ms"] + HOT["per_remote_layer_ms"] * remote
    return Candidate("dense+hot", {"expert_gib": round(expert_gib, 2)}, ms,
                     "fit 21 + 0.37 ms x remote layers (static 80 GiB 33.6 tok/s, dynamic 40.1 tok/s)",
                     {"remote_layers_per_token": round(remote, 1), "layer_all_local": round(local, 3),
                      "dynamic_copies": "not modelled (measured: 80 GiB static + 10 GiB dynamic, 16 copies/token, "
                                        "40.1 tok/s)"},
                     extrapolated=not low <= remote <= high,
                     measured_point={"tok_s": 33.6, "expert_budget_gib": 80, "placement": "W1 placement-pro80"}
                     if abs(expert_gib - (80 - HOT["reserve_gib"])) < 0.05 else None)


def candidates(vram_cap_gib, *, curve, front_k, margin_gib=None, link="1g"):
    """Every family at this cap, best first. front_k: the pipeline K that fits the cap (vramplan ds41-rtx-front);
    0 when not even one layer fits. The margin is never below max(2 GiB, 3 % of the cap)."""
    if not isinstance(vram_cap_gib, (int, float)) or not math.isfinite(vram_cap_gib) or vram_cap_gib <= 0:
        raise PlanError("vram cap must be a positive number of GiB")
    if margin_gib is not None and (not isinstance(margin_gib, (int, float)) or not math.isfinite(margin_gib)
                                   or margin_gib < 0):
        raise PlanError("margin must be a finite number of GiB >= 0")
    if type(front_k) is not int or front_k < 0:
        raise PlanError("front_k must be an integer >= 0")
    margin = max(2.0, 0.03 * vram_cap_gib, margin_gib or 0.0)
    out = [Candidate("pair-only", {}, 1000 / PAIR_ONLY_TOK_S, "measured P15 27.2 tok/s", {"rtx": "unused"},
                     estimate=False, measured_point={"tok_s": PAIR_ONLY_TOK_S})]
    if front_k >= 1:
        out.append(pipeline(min(front_k, LAYERS - 1), link))
    expert_gib = vram_cap_gib - margin - HOT["reserve_gib"]
    if expert_gib >= 0:
        out.append(hot(expert_gib, curve))
    return sorted(out, key=lambda c: c.ms_per_token)


def curve_from_csv(path, mode="replica"):
    """(expert GiB, layer_all_local) points from tests/fixtures/vramplan/ds41-hitrate.csv."""
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return [(float(r["expert_gib"]), float(r["layer_all_local"])) for r in csv.DictReader(f) if r["mode"] == mode]
