"""Turn the deterministic metric comparison into coaching narrative via FMAPI.

The LLM only *narrates* numbers computed in ``analysis.metrics`` — it is never
asked to invent statistics. If no serving endpoint is configured (local dev),
:func:`generate_feedback` returns a templated narrative so the app still works.

Per the product tenet (see config), coaching is framed around RANKED readiness:
unranked games are practice, and the advice tells the player what to drill in
unranked to climb in ranked.
"""

from __future__ import annotations

import hashlib
import os

from .metrics import MetricComparison

# In-process cache for FMAPI responses (#7). The narrative for a given
# (player window, role, tier) is deterministic enough to reuse across re-renders
# — toggling the role/tier or re-opening a tab no longer re-bills the endpoint.
# Bounded LRU-ish; a fast L1 in front of the optional durable L2 below.
_LLM_CACHE: dict[str, str] = {}
_LLM_CACHE_ORDER: list[str] = []
_LLM_CACHE_MAX = 256

# Optional durable, shared L2 cache (Lakebase). The app registers a (get, put)
# backend via set_cache_backend() at startup; when unset (tests, no Lakebase),
# only the in-process L1 is used. Kept as injected callables so this analysis
# module stays decoupled from the app's data layer.
_ext_get = None
_ext_put = None


def set_cache_backend(get_fn, put_fn) -> None:
    """Register a durable L2 cache: get_fn(key)->str|None, put_fn(key, value)."""
    global _ext_get, _ext_put
    _ext_get, _ext_put = get_fn, put_fn


def _l1_store(key: str, out: str) -> None:
    _LLM_CACHE[key] = out
    _LLM_CACHE_ORDER.append(key)
    if len(_LLM_CACHE_ORDER) > _LLM_CACHE_MAX:
        _LLM_CACHE.pop(_LLM_CACHE_ORDER.pop(0), None)


def _chat(endpoint: str, system: str, user: str, max_tokens: int,
          temperature: float = 0.3) -> str:
    """Query the FMAPI chat endpoint, caching on the exact prompt + params.

    Checks the in-process L1 cache, then the durable shared L2 (Lakebase, if
    registered), then the endpoint — populating both caches on a miss.
    """
    key = hashlib.sha256(
        f"{endpoint}\x00{max_tokens}\x00{temperature}\x00{system}\x00{user}".encode()
    ).hexdigest()
    cached = _LLM_CACHE.get(key)
    if cached is not None:
        return cached
    if _ext_get is not None:
        try:
            hit = _ext_get(key)
        except Exception:  # noqa: BLE001 — cache must never break inference
            hit = None
        if hit is not None:
            _l1_store(key, hit)
            return hit

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    w = WorkspaceClient()
    resp = w.serving_endpoints.query(
        name=endpoint,
        messages=[
            ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
            ChatMessage(role=ChatMessageRole.USER, content=user),
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    out = resp.choices[0].message.content
    _l1_store(key, out)
    if _ext_put is not None:
        try:
            _ext_put(key, out)
        except Exception:  # noqa: BLE001
            pass
    return out

SYSTEM_PROMPT = (
    "You are an expert League of Legends coach. Your single goal is to get the "
    "player READY TO CLIMB IN RANKED. Treat UNRANKED games as practice: the "
    "player should drill skills there that transfer to ranked. You receive the "
    "player's measured metrics over recent games — split into ranked and "
    "unranked — plus the benchmark for the tier they are trying to reach. Give "
    "concise, specific, actionable coaching. Reference the actual numbers; never "
    "invent statistics beyond those provided. Structure the response as: (1) a "
    "one-line ranked-readiness verdict, (2) 2-4 prioritized fundamentals "
    "blocking advancement, each with a concrete drill to practice IN UNRANKED "
    "games, (3) a note on whether their practice volume/balance (ranked vs "
    "unranked) supports climbing, and (4) one strength to keep leveraging."
)


def build_prompt(evaluation: dict, tier: str, readiness: dict | None = None) -> str:
    """Render the metric comparison + ranked/unranked split into the LLM prompt."""
    player = evaluation.get("player", {})
    lines = [
        f"Player role: {evaluation.get('role')}",
        f"Target ranked tier: {tier}",
        f"Games analyzed: {player.get('games', 0)}",
        f"Win rate: {player.get('winrate', 0):.0%}",
    ]
    if readiness:
        lines += [
            f"Ranked games: {readiness.get('ranked_games', 0)} | "
            f"Unranked (practice) games: {readiness.get('unranked_games', 0)} | "
            f"Practice ratio: {readiness.get('practice_ratio', 0):.0%} "
            f"({'enough' if readiness.get('practicing_enough') else 'too few practice games'})",
        ]
    lines += [
        "",
        f"Skill metrics (across all games) vs {tier} benchmark:",
        "Metric | Player | Benchmark | Delta",
        "------ | ------ | --------- | -----",
    ]
    for c in evaluation.get("comparisons", []):  # type: MetricComparison
        lines.append(
            f"{c.label} | {c.player:.2f} | {c.benchmark:.2f} | {c.delta_pct:+.0%}"
        )
    return "\n".join(lines)


def _template_feedback(evaluation: dict, tier: str, readiness: dict | None = None) -> str:
    """Deterministic fallback when no LLM endpoint is configured."""
    weaknesses = evaluation.get("weaknesses", [])
    strengths = evaluation.get("strengths", [])
    parts = [f"**Ranked-readiness summary vs {tier} benchmarks** "
             f"(no LLM endpoint configured):", ""]
    if readiness:
        verdict = ("practicing enough" if readiness.get("practicing_enough")
                   else "play more UNRANKED games to practice")
        parts.append(
            f"Practice balance: {readiness.get('unranked_games', 0)} unranked / "
            f"{readiness.get('ranked_games', 0)} ranked "
            f"({readiness.get('practice_ratio', 0):.0%} practice) — {verdict}."
        )
        parts.append("")
    if weaknesses:
        parts.append("**Drill these in unranked to climb:**")
        for c in weaknesses:
            parts.append(f"- {c.label}: {c.player:.2f} vs {c.benchmark:.2f} ({c.delta_pct:+.0%})")
    else:
        parts.append("No metric is more than 10% below benchmark — solid all-around.")
    if strengths:
        parts.append("")
        parts.append("**Strengths to keep:**")
        for c in strengths:
            parts.append(f"- {c.label}: {c.player:.2f} vs {c.benchmark:.2f} ({c.delta_pct:+.0%})")
    return "\n".join(parts)


METRIC_SYSTEM_PROMPT = (
    "You are an expert League of Legends coach helping a player get ready to "
    "climb in ranked. You receive a table of the player's per-game averages for "
    "specific metrics alongside the average for a GOLD-tier player in the same "
    "role. For EACH metric, write ONE short, specific, actionable sentence — "
    "focused on areas of improvement. If the player is below the GOLD average, "
    "say what to do to close the gap (a concrete habit/drill). If at or above, "
    "briefly affirm it. Do not invent numbers. Respond with ONLY a JSON object "
    "mapping each metric key to its sentence, no prose around it."
)


def _fmt(v, fmt):
    if v is None:
        return "n/a"
    return f"{v:.0%}" if fmt == "pct" else f"{v:.2f}"


def _template_metric_analysis(comparisons: list[dict]) -> dict:
    """Deterministic per-metric note when no LLM endpoint is configured."""
    out = {}
    for c in comparisons:
        if c["verdict"] == "no_data":
            out[c["key"]] = "Not enough data yet."
        elif c["verdict"] == "below":
            out[c["key"]] = (
                f"Below the Gold average ({_fmt(c['player'], c['fmt'])} vs "
                f"{_fmt(c['gold'], c['fmt'])}) — a priority to improve.")
        elif c["verdict"] == "above":
            out[c["key"]] = "At or above Gold level — keep it up."
        else:
            out[c["key"]] = "Roughly Gold level."
    return out


def analyze_metrics(
    comparisons: list[dict],
    tier: str = "GOLD",
    endpoint: str | None = None,
) -> dict:
    """Return {metric_key: analysis sentence} for the Detailed Metrics view.

    One LLM call (structured JSON) over all metrics; templated fallback when no
    endpoint is set or the call/parse fails.
    """
    endpoint = endpoint or os.environ.get("SERVING_ENDPOINT", "")
    if not endpoint or not comparisons:
        return _template_metric_analysis(comparisons)

    lines = [f"Role cohort: {tier}", "key | metric | player | gold | status"]
    for c in comparisons:
        lines.append(
            f"{c['key']} | {c['label']} | {_fmt(c['player'], c['fmt'])} | "
            f"{_fmt(c['gold'], c['fmt'])} | {c['verdict']}")
    try:
        import json

        content = _chat(endpoint, METRIC_SYSTEM_PROMPT, "\n".join(lines),
                        max_tokens=1200, temperature=0.2).strip()
        if content.startswith("```"):
            content = content.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(content)
        # Backfill any missing keys from the template.
        fallback = _template_metric_analysis(comparisons)
        return {c["key"]: parsed.get(c["key"]) or fallback[c["key"]] for c in comparisons}
    except Exception:
        return _template_metric_analysis(comparisons)


ITEM_SYSTEM_PROMPT = (
    "You are an expert League of Legends coach reviewing ONE game's itemization. "
    "You are given the player's champion and role, their starting items, their "
    "full purchase order, their final build, and their lane opponent's champion "
    "and final build. Judge the itemization for THIS matchup. Cover: (1) whether "
    "the starting items fit the champion and lane, (2) whether the build responds "
    "to the opponent's champion and items — call out missing defensive choices "
    "(armor vs an AD threat, magic resist vs an AP threat, anti-heal vs healing "
    "champions, boots choice, anti-tank/armor-pen vs tanks), (3) build-order "
    "efficiency (power spikes, components vs gold sinks). Then give 2-3 concrete, "
    "prioritized recommendations naming specific items to buy or swap in this "
    "matchup. Be specific and reference the actual items by name. Use short "
    "markdown sections with bold headers. Do not invent items the player didn't "
    "build; reason only from what's provided."
)


def _template_itemization(facts: dict) -> str:
    """Deterministic itemization summary when no LLM endpoint is configured."""
    p = facts.get("player", {})
    opp = facts.get("opponent")
    start = facts.get("starting_items_named") or []
    final = facts.get("final_items_named") or []
    lines = [f"**Itemization review (no LLM endpoint configured)**", ""]
    lines.append(f"- **Champion / role:** {p.get('champion', '?')} "
                 f"({p.get('role') or 'unknown lane'}) — "
                 f"{'Win' if p.get('win') else 'Loss'}")
    lines.append(f"- **Starting items:** {', '.join(start) or '—'}")
    lines.append(f"- **Final build:** {', '.join(final) or '—'}")
    if opp:
        opp_final = facts.get("opponent_items_named") or []
        lines.append(f"- **Lane opponent:** {opp.get('champion', '?')} — "
                     f"built {', '.join(opp_final) or '—'}")
        lines.append("")
        lines.append(f"Review your defensive items against **{opp.get('champion', '?')}** "
                     f"— make sure you bought the right resistances (armor vs AD, magic "
                     f"resist vs AP) and anti-heal if they sustain. Connect a serving "
                     f"endpoint for a full AI counter-build breakdown.")
    else:
        lines.append("")
        lines.append("No lane opponent detected for this game mode, so no matchup-based "
                     "counter-build advice. Connect a serving endpoint for richer analysis.")
    return "\n".join(lines)


def _itemization_prompt(facts: dict) -> str:
    p = facts.get("player", {})
    opp = facts.get("opponent")
    lines = [
        f"Player champion: {p.get('champion', '?')}",
        f"Player role: {p.get('role') or 'unknown'}",
        f"Result: {'Win' if p.get('win') else 'Loss'}",
        f"Starting items: {', '.join(facts.get('starting_items_named') or []) or 'none recorded'}",
        f"Full purchase order: {', '.join(facts.get('purchase_order_named') or []) or 'none recorded'}",
        f"Final build: {', '.join(facts.get('final_items_named') or []) or 'none'}",
    ]
    if opp:
        lines += [
            f"Lane opponent champion: {opp.get('champion', '?')}",
            f"Lane opponent final build: {', '.join(facts.get('opponent_items_named') or []) or 'none'}",
        ]
    else:
        lines.append("Lane opponent: none (no-lane game mode)")
    return "\n".join(lines)


def analyze_itemization(facts: dict, endpoint: str | None = None) -> str:
    """Coaching verdict on one game's itemization (FMAPI; templated fallback).

    ``facts`` must already carry the *_named lists (item ids resolved to names).
    """
    if not facts or not facts.get("player"):
        return "No itemization data for this game."
    endpoint = endpoint or os.environ.get("SERVING_ENDPOINT", "")
    if not endpoint:
        return _template_itemization(facts)
    try:
        # max_tokens=2000: the multi-section review + tables overrun a smaller cap.
        return _chat(endpoint, ITEM_SYSTEM_PROMPT, _itemization_prompt(facts),
                     max_tokens=2000)
    except Exception as exc:  # pragma: no cover - runtime-only path
        return f"{_template_itemization(facts)}\n\n_(LLM call failed: {exc})_"


TREND_SYSTEM_PROMPT = (
    "You are an expert League of Legends coach reviewing a player's ITEMIZATION "
    "HABITS across their recent games (not a single game). You are given aggregate "
    "rates — how often they completed boots, built armor, built magic resist, "
    "built anti-heal (Grievous Wounds), and left unfinished components in the "
    "build — plus their average number of completed items, how varied their "
    "opening buy is, and the champions/matchups. Identify the 3-4 most important "
    "RECURRING itemization mistakes or habits and give a concrete fix for each "
    "(name items and triggers, e.g. 'buy Mercury's Treads vs AP-heavy teams'). "
    "Then note one good habit to keep. Use short markdown sections with bold "
    "headers. Reason only from the provided rates; do not invent statistics."
)


def _template_item_trends(trends: dict) -> str:
    n = trends.get("games", 0)
    if not n:
        return "Not enough games to analyze itemization trends yet."

    def pct(k):
        return f"{trends.get(k, 0):.0%}"

    parts = [f"**Itemization habits across your last {n} games** "
             f"(no LLM endpoint configured):", ""]
    parts.append(f"- Completed boots in **{pct('boots_rate')}** of games")
    parts.append(f"- Built an armor item in **{pct('armor_rate')}**, magic resist in "
                 f"**{pct('mr_rate')}**")
    parts.append(f"- Bought anti-heal (Grievous Wounds) in **{pct('antiheal_rate')}**")
    parts.append(f"- Left an unfinished component in the build in **{pct('component_rate')}**")
    parts.append(f"- Averaged **{trends.get('avg_real_items', 0):.1f}** completed items; "
                 f"opening buy varied across **{trends.get('start_variety', 0)}** sets")
    start, cnt = trends.get("most_common_start", ("—", 0))
    parts.append(f"- Most common start: **{start}** ({cnt}/{n} games)")
    parts.append("")
    parts.append("Connect a serving endpoint for prioritized AI coaching on these habits.")
    return "\n".join(parts)


def _item_trends_prompt(trends: dict, games: list[dict]) -> str:
    lines = [
        f"Games analyzed: {trends.get('games')} ({trends.get('wins')} wins)",
        f"Boots completed rate: {trends.get('boots_rate', 0):.0%}",
        f"Armor item rate: {trends.get('armor_rate', 0):.0%}",
        f"Magic resist rate: {trends.get('mr_rate', 0):.0%}",
        f"Anti-heal (Grievous Wounds) rate: {trends.get('antiheal_rate', 0):.0%}",
        f"Unfinished-component-in-build rate: {trends.get('component_rate', 0):.0%}",
        f"Average completed items: {trends.get('avg_real_items', 0):.1f}",
        f"Distinct opening buys: {trends.get('start_variety', 0)}",
        f"Most common start: {trends.get('most_common_start', ('—', 0))[0]}",
        "",
        "Per-game (champion vs lane opponent — start):",
    ]
    for g in games:
        lines.append(
            f"- {g.get('champion', '?')} vs {g.get('opponent_champion') or 'unknown'} "
            f"({'W' if g.get('win') else 'L'}) — start: "
            f"{', '.join(g.get('starting_named') or []) or 'none'}")
    return "\n".join(lines)


def analyze_item_trends(trends: dict, games: list[dict], endpoint: str | None = None) -> str:
    """Coaching narrative on recurring itemization habits (FMAPI; templated fallback)."""
    if not trends or not trends.get("games"):
        return "Not enough games to analyze itemization trends yet."
    endpoint = endpoint or os.environ.get("SERVING_ENDPOINT", "")
    if not endpoint:
        return _template_item_trends(trends)
    try:
        return _chat(endpoint, TREND_SYSTEM_PROMPT, _item_trends_prompt(trends, games),
                     max_tokens=1400)
    except Exception as exc:  # pragma: no cover - runtime-only path
        return f"{_template_item_trends(trends)}\n\n_(LLM call failed: {exc})_"


MACRO_SYSTEM_PROMPT = (
    "You are an expert League of Legends coach reviewing ONE game at the MACRO / "
    "team level — not individual mechanics. You receive computed facts for BOTH "
    "teams (blue and red): objectives and their timings, gold/XP swing over time, "
    "vision, each champion's damage profile (who the real AD/AP threats are), "
    "whether players itemized resistances against those threats, lane outcomes at "
    "14 minutes, teamfight results, death/trade patterns, and LOW-CONFIDENCE "
    "positional inferences (support roam %, split-push isolation %). If a focus "
    "team is given, coach THAT team (and reference the enemy as context) — whether "
    "they won or lost, explicitly CREDIT what they did well (especially the "
    "winning team's strengths) AND call out where they could still have done "
    "better; otherwise give a balanced both-teams review. Cover, with specific "
    "numbers: "
    "(1) objective prioritization & trades, (2) map control / vision, (3) the "
    "gold/XP swing and whether a lead was thrown or a comeback made, (4) team "
    "itemization vs the enemy's damage threats (armor vs AD, magic resist vs AP, "
    "anti-heal), (5) teamfighting, (6) lane outcomes, and (7) the inferred "
    "strategic reads (split-push, support roaming, wave control) — clearly mark "
    "these as inferred/low-confidence. End with the 3 highest-impact macro "
    "lessons. Use short markdown sections with bold headers. Reason ONLY from the "
    "provided facts; never invent numbers."
)


def _side(facts: dict, team_id: int) -> str:
    return (facts.get("overview", {}).get(team_id, {}) or {}).get("side", str(team_id))


def _macro_prompt(facts: dict) -> str:
    ov, obj = facts.get("overview", {}), facts.get("objectives", {})
    dmg, vis = facts.get("damage", {}), facts.get("vision", {})
    lanes, tf = facts.get("lanes", []), facts.get("teamfights", {})
    deaths, pos = facts.get("deaths", {}), facts.get("positions", {})
    gx = facts.get("gold_xp", {}).get("summary", {})
    itemz = facts.get("itemization", {})
    focus = facts.get("focus_team")
    lines = [f"Match: {facts.get('match_id')} | duration {facts.get('duration_min')} min",
             f"Focus team: {_side(facts, focus) if focus else 'none (neutral review)'}", ""]
    for tid in (100, 200):
        o = ov.get(tid, {})
        ob = obj.get(tid, {})
        v = vis.get(tid, {})
        d = dmg.get(tid, {})
        lines.append(f"=== {o.get('side')} team — {'WIN' if o.get('win') else 'LOSS'} ===")
        lines.append(f"Champions: {o.get('champions')}")
        lines.append(f"Kills {o.get('kills')} | gold {o.get('gold')}")
        lines.append(f"Objectives: {ob.get('dragons')} drakes "
                     f"({', '.join(ob.get('dragon_types') or []) or 'none'}"
                     f"{', SOUL' if ob.get('soul') else ''}), {ob.get('heralds')} herald, "
                     f"{ob.get('grubs')} grubs, {ob.get('barons')} baron, {ob.get('towers')} towers"
                     f"{', first blood' if ob.get('first_blood') else ''}"
                     f"{', first tower' if ob.get('first_tower') else ''}")
        lines.append(f"Vision: score {v.get('vision_score')}, wards {v.get('wards_placed')} placed / "
                     f"{v.get('wards_killed')} killed, {v.get('control_wards')} control")
        ad, ap = d.get("biggest_ad"), d.get("biggest_ap")
        lines.append(f"Top AD threat: {ad['champion'] if ad else '—'} "
                     f"({ad['phys'] if ad else 0} phys); top AP threat: "
                     f"{ap['champion'] if ap else '—'} ({ap['magic'] if ap else 0} magic)")
        if itemz.get(tid):
            lines.append(f"Resistances vs enemy threats: {itemz[tid]}")
        lines.append(f"Deaths {deaths.get(tid, {}).get('deaths')} "
                     f"({deaths.get(tid, {}).get('deaths_without_trade')} without a trade)")
        lines.append("")
    lines.append(f"Gold swing: max blue lead {gx.get('max_blue_lead')}, max red lead "
                 f"{gx.get('max_red_lead')}, final diff {gx.get('final_gold_diff')}, "
                 f"swing minute {gx.get('swing_minute')}, lead thrown: {gx.get('threw_lead')}")
    lines.append(f"Teamfights: {tf.get('summary')}")
    lines.append("Lane outcomes @14 (gold diff, +=blue): " +
                 "; ".join(f"{l['role']} {l['blue']}v{l['red']} {l['gold_diff']}" for l in lanes))
    lines.append(f"Support roam ratio (pre-14m away from ADC): {pos.get('support_roam')}")
    iso = {pid: f"{r['champion']} {r['isolated_ratio']}"
           for pid, r in (pos.get("isolation") or {}).items() if r["isolated_ratio"] > 0.4}
    lines.append(f"High post-15m isolation (split-push proxy): {iso or 'none notable'}")
    return "\n".join(lines)


def _template_macro(facts: dict) -> str:
    ov = facts.get("overview", {})
    obj = facts.get("objectives", {})
    gx = facts.get("gold_xp", {}).get("summary", {})
    tf = facts.get("teamfights", {}).get("summary", {})
    blue, red = ov.get(100, {}), ov.get(200, {})
    winner = "Blue" if blue.get("win") else "Red"
    parts = ["**Macro game review (no LLM endpoint configured)**", "",
             f"- **Result:** {winner} win — {blue.get('kills', 0)}/{red.get('kills', 0)} kills",
             f"- **Objectives:** Blue {obj.get(100, {}).get('dragons')} drakes / "
             f"{obj.get(100, {}).get('barons')} baron / {obj.get(100, {}).get('towers')} towers vs "
             f"Red {obj.get(200, {}).get('dragons')} / {obj.get(200, {}).get('barons')} / "
             f"{obj.get(200, {}).get('towers')}",
             f"- **Gold swing:** max Blue +{gx.get('max_blue_lead', 0)}, max Red +"
             f"{gx.get('max_red_lead', 0)}, lead thrown: {gx.get('threw_lead')}",
             f"- **Teamfights:** Blue won {tf.get('blue_won', 0)}, Red won {tf.get('red_won', 0)}, "
             f"aces {tf.get('aces', 0)}", "",
             "Connect a serving endpoint for the full AI macro breakdown."]
    return "\n".join(parts)


def analyze_macro(facts: dict, endpoint: str | None = None) -> str:
    """Macro/game-level coaching for one match (FMAPI; templated fallback)."""
    if not facts or not facts.get("overview"):
        return "No macro data for this game."
    endpoint = endpoint or os.environ.get("SERVING_ENDPOINT", "")
    if not endpoint:
        return _template_macro(facts)
    try:
        return _chat(endpoint, MACRO_SYSTEM_PROMPT, _macro_prompt(facts), max_tokens=2600)
    except Exception as exc:  # pragma: no cover - runtime-only path
        return f"{_template_macro(facts)}\n\n_(LLM call failed: {exc})_"


def generate_feedback(
    evaluation: dict,
    tier: str,
    readiness: dict | None = None,
    endpoint: str | None = None,
) -> str:
    """Generate ranked-readiness coaching narrative.

    Uses the Databricks FMAPI serving endpoint when available; otherwise returns
    the templated fallback. ``endpoint`` defaults to the SERVING_ENDPOINT env var.
    """
    if not evaluation.get("player"):
        return "No matches available yet. Run an ingestion refresh to load games."

    if not evaluation.get("has_benchmark"):
        return (f"**No {tier} benchmark data for the {evaluation.get('role')} role yet**, so "
                f"there's nothing to compare against at this tier/role. Pick a different "
                f"Target Tier or role that has data — the coaching below needs a benchmark "
                f"to measure you against.")

    endpoint = endpoint or os.environ.get("SERVING_ENDPOINT", "")
    if not endpoint:
        return _template_feedback(evaluation, tier, readiness)

    try:
        return _chat(endpoint, SYSTEM_PROMPT, build_prompt(evaluation, tier, readiness),
                     max_tokens=700)
    except Exception as exc:  # pragma: no cover - runtime-only path
        return f"{_template_feedback(evaluation, tier, readiness)}\n\n_(LLM call failed: {exc})_"
