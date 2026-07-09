from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from storage.db import _mongo_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def _month_key(ts: datetime) -> str:
    return ts.strftime("%Y-%m")


def _collection():
    return _mongo_db()["automation_analytics"]


def init_analytics() -> None:
    collection = _collection()
    collection.create_index("scope")
    collection.create_index("period")
    collection.create_index("bot_name")
    collection.create_index("updated_at")


def _base_updates(event: str, ts: datetime, bot_name: str | None) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "$inc": {event: 1},
        "$set": {"updated_at": ts},
        "$setOnInsert": {"created_at": ts},
    }
    if bot_name is not None:
        updates["$set"]["bot_name"] = bot_name
    return updates


def record_event(event: str, *, bot_name: str | None = None, runtime_seconds: float | None = None) -> None:
    ts = _now()
    collection = _collection()
    scopes = [
        {"_id": "overall", "scope": "overall", "period": "overall"},
        {"_id": f"day:{_day_key(ts)}", "scope": "day", "period": _day_key(ts), "day": _day_key(ts)},
        {"_id": f"month:{_month_key(ts)}", "scope": "month", "period": _month_key(ts), "month": _month_key(ts)},
    ]
    if bot_name:
        scopes.append({"_id": f"bot:{bot_name}", "scope": "bot", "bot_name": bot_name})
    for filter_doc in scopes:
        updates = _base_updates(event, ts, bot_name if filter_doc.get("scope") == "bot" else None)
        if runtime_seconds is not None and event in {"cycle_completed", "cycle_failed"}:
            updates["$inc"]["runtime_seconds"] = float(runtime_seconds)
        if event == "cycle_started":
            updates["$setOnInsert"]["first_automation_start"] = ts
            updates["$set"]["last_cycle_started_at"] = ts
        if event == "cycle_completed":
            updates["$set"]["last_cycle_completed_at"] = ts
        if event == "match_found":
            updates["$set"]["last_match_at"] = ts
        if event in {"promotion_sent", "sticker_sent", "stop_command_sent", "security_challenge", "security_bypass_used"}:
            updates["$set"]["last_activity_at"] = ts
        collection.update_one(filter_doc, updates, upsert=True)


def get_scope_document(scope: str, period: str | None = None, bot_name: str | None = None) -> dict[str, Any] | None:
    query: dict[str, Any] = {"scope": scope}
    if period is not None:
        query["period"] = period
    if bot_name is not None:
        query["bot_name"] = bot_name
    return _collection().find_one(query, {"_id": 0})


def list_bot_documents() -> list[dict[str, Any]]:
    return list(_collection().find({"scope": "bot"}, {"_id": 0}).sort("bot_name", 1))

