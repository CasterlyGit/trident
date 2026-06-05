"""trident formulas — single source of every curve (DESIGN.md §6 / ARCHITECTURE.md lever table).

Imported by mint.py ONLY. Consumers read pre-baked values from wallet.json and do zero math.

Endpoint contracts:
- L=100 = free lane: no lever damping anywhere; only real headroom H still routes.
- L=0   = minimum viable shape (haiku / W=1 / think=0 / handoff-only inject), never a halt.
- V floor is 1 ALWAYS — the routing/rigor boundary.
"""

import math

TIERS = ["haiku", "sonnet", "opus"]  # ascending


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

    Calibration (lever table): L=100→opus · L=63→sonnet · L=30→sonnet(rare)/haiku-default
    · L=0→haiku. At L=100 only real headroom routes (free lane ≠ hide budget reality).
    """
    score = m * (h_eff / 100.0)
    if L_eff <= 0:
        return "haiku"
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
    envelope = (
        f"[TRIDENT L={L} H={h_eff:.0f} tier≤{spawn} W≤{w} "
        f"think≤{_fmt_k(think)} inject≤{_fmt_k(inject)} V≥{v} spec={'on' if spec else 'off'}]"
    )
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
