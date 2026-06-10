"""trident formulas — single source of every curve (DESIGN.md §6 / ARCHITECTURE.md lever table).

Imported by mint.py ONLY. Consumers read pre-baked values from wallet.json and do zero math.

Endpoint contracts:
- L=100 = free lane: no lever damping anywhere; only real headroom H still routes.
- L=0   = minimum viable shape (haiku / W=1 / think=0 / handoff-only inject), never a halt.
- V floor is 1 ALWAYS — the routing/rigor boundary.
"""

import math

TIERS = ["haiku", "sonnet", "opus", "fable"]  # ascending
TIER_MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
}


def M(L_eff):
    """Lever multiplier. M(100)=1 (free lane), M(0)=0."""
    L = max(0, min(100, L_eff))
    return (L / 100.0) ** 1.5


def headroom_raw(five_h_left, seven_d_left, p_pool_left=None, secs_to_reset=None):
    """H_raw = min of pool lefts + imminence bonus (+40pp if reset<30m, +20 if <60m), cap 100."""
    pools = [p for p in (five_h_left, seven_d_left, p_pool_left) if p is not None]
    h = min(pools) if pools else 50.0  # no telemetry → assume middling, fail open
    if secs_to_reset is not None:
        if secs_to_reset <= 1800:
            h += 40
        elif secs_to_reset <= 3600:
            h += 20
    return min(100.0, max(0.0, h))


def headroom_eff(h_raw, active_sessions):
    """Dampened fair share across open windows (sqrt, not linear — idle ≠ spending)."""
    n = max(1, active_sessions)
    return min(h_raw, 100.0) / math.sqrt(n)


def tier_spawn(m, h_eff, L_eff):
    """Spawn model ceiling for a depth-0 session. Tier curve on M·H_eff.

    Calibration (lever table): L=100+H≥90→fable · L=100→opus · L=63→sonnet ·
    L=30→sonnet(rare)/haiku-default · L=0→haiku. At L=100 only real headroom routes
    (free lane ≠ hide budget reality). Fable band is deliberately narrow: free lane
    AND near-full effective headroom (single window, fat pool) — the ceiling is
    permissive (it stops capping opus/fable requests), it never forces fable.
    """
    score = m * (h_eff / 100.0)
    if L_eff <= 0:
        return "haiku"
    if score >= 0.9:
        return "fable"
    if score >= 0.6:
        return "opus"
    if score >= 0.05:
        return "sonnet"
    return "haiku"


def tier_child(spawn_tier):
    """Depth cascade: a session that is itself a subagent spawns one tier lower."""
    i = TIERS.index(spawn_tier)
    return TIERS[max(0, i - 1)]


def fanout_max(m, h_eff):
    return max(1, math.floor(12 * m * (h_eff / 100.0) ** 1.2))


def thinking_budget(m, h_eff):
    """ADVISORY (envelope only) — quadratic in headroom: first luxury shed."""
    return int(32000 * m * (h_eff / 100.0) ** 2)


def inject_cap_tokens(m):
    """MECHANICAL — concave: context degrades slowly. Rank-0 active-handoff chunk is exempt."""
    return int(4000 * m ** 0.6)


def verify_min(m, h_eff):
    """Floor 1 ALWAYS — never goes to zero."""
    return max(1, round(5 * m * h_eff / 100.0))


def speculative(m, h_eff):
    return (m * h_eff) > 15


def compact_scale(m):
    """Multiplier on shed guard thresholds — fires earlier when tight."""
    return 0.5 + m / 2.0


def roi_min(m):
    """evolve-v2 ROI admission bar. Capped at 6 — learning never locks out."""
    if m <= 0:
        return 6.0
    return min(1.2 / m, 6.0)


def _fmt_k(n):
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k" if n >= 1000 else str(n)


def render_envelope(L, h_eff, spawn, w, think, inject, v, spec, brain=False):
    return (
        f"[TRIDENT L={L} H={h_eff:.0f} tier≤{spawn} W≤{w} "
        f"think≤{_fmt_k(think)} inject≤{_fmt_k(inject)} V≥{v} spec={'on' if spec else 'off'}"
        f"{' 🧠' if brain else ''}]"
    )


def derived_block(L_eff, h_raw, active_sessions):
    """Compute one fully-baked derived block (DESIGN.md §2 shape) for a given L_eff."""
    L = max(0, min(100, int(L_eff)))
    m = M(L)
    h_eff = headroom_eff(h_raw, active_sessions)
    # Free lane: at L=100 skip lever damping entirely — only H routes.
    spawn = tier_spawn(m, h_eff, L)
    child = tier_child(spawn)
    w = fanout_max(m, h_eff)
    think = thinking_budget(m, h_eff)
    inject = inject_cap_tokens(m)
    v = verify_min(m, h_eff)
    spec = speculative(m, h_eff)
    envelope = render_envelope(L, h_eff, spawn, w, think, inject, v, spec)
    return {
        "L_eff": L,
        "M": round(m, 3),
        "H": round(h_eff, 1),
        "tier_ceiling_spawn": spawn,
        "tier_ceiling_child": child,
        "fanout_max": w,
        "thinking_budget": think,
        "inject_cap_tokens": inject,
        "verify_min": v,
        "speculative": spec,
        "compact_scale": round(compact_scale(m), 2),
        "roi_min": round(roi_min(m), 1),
        "envelope": envelope,
    }


# ----------------------------------------------------------- burn forecasting

def burn_forecast(points, left_now, secs_to_reset):
    """Deterministic burn-rate fit over recent history. Pure math, no I/O.

    points: [(ts_epoch, five_h_left_pct)] — recent-window samples, any order.
    Returns {"burn_pph", "eta_exhaust_min", "wall_before_reset"} or None when the
    data can't support a fit (few points, tiny span, refill mid-window).
    """
    pts = sorted((p for p in points if p[1] is not None), key=lambda p: p[0])
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < 600:
        return None
    n = len(pts)
    t0 = pts[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in pts]
    ys = [l for _, l in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den  # pp per hour
    burn_pph = -slope
    if burn_pph < 0.5:  # idle or window refill — no meaningful wall ETA
        return {"burn_pph": round(max(0.0, burn_pph), 1),
                "eta_exhaust_min": None, "wall_before_reset": False}
    eta_min = (left_now / burn_pph) * 60.0
    return {
        "burn_pph": round(burn_pph, 1),
        "eta_exhaust_min": int(eta_min),
        "wall_before_reset": bool(secs_to_reset and eta_min * 60 < secs_to_reset),
    }


# ------------------------------------------------------- brain policy overlay

POLICY_SCHEMA_VERSION = 1
# Hard clamps applied IN CODE no matter what the policy file says. The brain can
# bias routing inside these rails; it can never reach correctness dimensions:
# verify_min / roi_min / L_eff / H are not overlay-addressable at all.
POLICY_CLAMPS = {
    "tier_bias": (-1, 1),        # shift spawn ceiling by at most one tier
    "width_mult": (0.5, 1.5),
    "think_mult": (0.5, 1.5),
    "inject_mult": (0.6, 1.25),
    "compact_bias": (0.75, 1.25),
}
INJECT_FLOOR = 800  # active-handoff chunk is rank-0/exempt anyway; belt + suspenders


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def validate_policy(doc, now):
    """Return a clamped policy dict, or None if the doc is unusable (fail open).

    Unusable = wrong schema, expired, or no parseable knobs. Every knob is
    individually optional; garbage knobs are dropped, not fatal.
    """
    if not isinstance(doc, dict) or doc.get("schema_version") != POLICY_SCHEMA_VERSION:
        return None
    try:
        if now >= float(doc.get("expires_at", 0)):
            return None
    except (TypeError, ValueError):
        return None
    raw = doc.get("policy")
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, (lo, hi) in POLICY_CLAMPS.items():
        try:
            if k in raw:
                v = float(raw[k])
                out[k] = int(_clamp(round(v), lo, hi)) if k == "tier_bias" else _clamp(v, lo, hi)
        except (TypeError, ValueError):
            continue
    if isinstance(raw.get("spec_override"), bool):
        out["spec_override"] = raw["spec_override"]
    return out or None


def apply_policy(block, policy):
    """Fold a validated policy into a derived block. Pure; returns a new dict.

    Bright line: verify_min, roi_min, L_eff, M, H pass through untouched.
    """
    b = dict(block)
    spawn = b["tier_ceiling_spawn"]
    bias = policy.get("tier_bias", 0)
    if bias:
        idx = _clamp(TIERS.index(spawn) + bias, 0, len(TIERS) - 1)
        spawn = TIERS[idx]
    w = max(1, round(b["fanout_max"] * policy.get("width_mult", 1.0)))
    think = int(b["thinking_budget"] * policy.get("think_mult", 1.0))
    inject = max(INJECT_FLOOR, int(b["inject_cap_tokens"] * policy.get("inject_mult", 1.0)))
    spec = policy["spec_override"] if "spec_override" in policy else b["speculative"]
    b.update(
        tier_ceiling_spawn=spawn,
        tier_ceiling_child=tier_child(spawn),
        fanout_max=w,
        thinking_budget=think,
        inject_cap_tokens=inject,
        speculative=spec,
        compact_scale=round(_clamp(b["compact_scale"] * policy.get("compact_bias", 1.0), 0.5, 1.0), 2),
        brain=True,
        envelope=render_envelope(b["L_eff"], b["H"], spawn, w, think, inject,
                                 b["verify_min"], spec, brain=True),
    )
    return b
