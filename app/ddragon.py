"""Data Dragon static-data lookups: item-id -> name + icon URL, champion icons.

Data Dragon is Riot's public static CDN (no API key, no rate limit). We fetch the
latest version and the item catalog once per process and cache them in memory.
The app's egress check already confirms this host is reachable.
"""

from __future__ import annotations

import json
import urllib.request

_BASE = "https://ddragon.leagueoflegends.com"
_TIMEOUT = 8

_version: str | None = None
_items: dict[str, dict] | None = None  # {item_id_str: {name, ...}}

# Champion id fixups: match-v5 championName mostly equals the Data Dragon id, but
# a few diverge and would 404 on the icon CDN.
_CHAMP_FIXUP = {"Wukong": "MonkeyKing", "FiddleSticks": "Fiddlesticks"}


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "league-ai-coach"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def version() -> str:
    """Latest Data Dragon version (e.g. "14.23.1"); cached, falls back to a pin."""
    global _version
    if _version is None:
        try:
            _version = _fetch_json(f"{_BASE}/api/versions.json")[0]
        except Exception:
            _version = "14.23.1"  # reasonable fallback if the CDN is unreachable
    return _version


def _item_catalog() -> dict[str, dict]:
    global _items
    if _items is None:
        try:
            data = _fetch_json(f"{_BASE}/cdn/{version()}/data/en_US/item.json")
            _items = data.get("data", {}) or {}
        except Exception:
            _items = {}
    return _items


def item_name(item_id) -> str:
    """Display name for an item id ("Item <id>" if unknown/uncatalogued)."""
    entry = _item_catalog().get(str(item_id))
    return entry["name"] if entry and entry.get("name") else f"Item {item_id}"


def item_icon(item_id) -> str:
    return f"{_BASE}/cdn/{version()}/img/item/{item_id}.png"


def champion_icon(champion_name: str) -> str:
    cid = _CHAMP_FIXUP.get(champion_name, champion_name or "")
    return f"{_BASE}/cdn/{version()}/img/champion/{cid}.png"


def decorate_items(item_ids) -> list[dict]:
    """[{id, name, icon}] for a list of item ids, in order."""
    return [{"id": i, "name": item_name(i), "icon": item_icon(i)} for i in (item_ids or [])]


def classify_item(item_id) -> dict:
    """Classify an item for build-trend analysis using its Data Dragon tags.

    Returns {boots, armor, mr, antiheal, component, real}. Data Dragon uses the
    tag "SpellBlock" for magic resist; anti-heal (Grievous Wounds) is detected
    from the item description. ``component`` is an unfinished item (it can still
    build "into" something) left in the final build; ``real`` excludes
    consumables, trinkets and wards so we count actual build items.
    """
    entry = _item_catalog().get(str(item_id))
    if not entry:
        return {"boots": False, "armor": False, "mr": False, "antiheal": False,
                "component": False, "real": False}
    tags = entry.get("tags", []) or []
    desc = (entry.get("description") or "") + (entry.get("plaintext") or "")
    real = not ({"Consumable", "Trinket", "Vision"} & set(tags))
    return {
        "boots": "Boots" in tags,
        "armor": "Armor" in tags,
        "mr": "SpellBlock" in tags,
        "antiheal": "Grievous Wounds" in desc,
        "component": real and bool(entry.get("into")),
        "real": real,
    }
