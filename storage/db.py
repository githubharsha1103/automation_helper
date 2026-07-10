import json
import logging
import os
import time
import asyncio
from datetime import datetime
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
logger = logging.getLogger(__name__)

_MONGO_CLIENT: MongoClient | None = None
_MONGO_DB = None
_SETTING_CACHE: dict[str, Any] = {}
_BOT_CACHE: dict[str, dict[str, Any]] = {}
_GROUP_CACHE: dict[str, dict[str, Any]] = {}
_MESSAGE_CACHE: dict[int, dict[str, Any]] = {}
_DB_METRICS: dict[str, dict[str, int]] = {
    "get_setting": {"hits": 0, "misses": 0, "count": 0},
    "get_bot": {"hits": 0, "misses": 0, "count": 0},
    "list_groups": {"hits": 0, "misses": 0, "count": 0},
    "list_messages": {"hits": 0, "misses": 0, "count": 0},
}
_DB_LAST_TIMINGS: dict[str, float] = {}
_OP_HISTORY: list[dict[str, Any]] = []
_OP_STATS: dict[str, dict[str, Any]] = {}


def record_operation(
    name: str,
    elapsed_ms: float,
    success: bool = True,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "name": name,
        "category": category or "general",
        "elapsed_ms": round(elapsed_ms, 2),
        "success": success,
        "metadata": metadata or {},
    }
    _OP_HISTORY.append(entry)
    if len(_OP_HISTORY) > 100:
        del _OP_HISTORY[:-100]
    stats = _OP_STATS.setdefault(
        name,
        {"count": 0, "errors": 0, "total_ms": 0.0, "slowest_ms": 0.0},
    )
    stats["count"] += 1
    stats["total_ms"] += elapsed_ms
    stats["slowest_ms"] = max(stats["slowest_ms"], elapsed_ms)
    if not success:
        stats["errors"] += 1


def telemetry_snapshot() -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for name, stats in sorted(_OP_STATS.items(), key=lambda item: item[1]["slowest_ms"], reverse=True):
        count = stats["count"] or 1
        operations.append(
            {
                "name": name,
                "count": stats["count"],
                "errors": stats["errors"],
                "error_rate": round(stats["errors"] / count, 4),
                "avg_ms": round(stats["total_ms"] / count, 2),
                "slowest_ms": round(stats["slowest_ms"], 2),
            }
        )
    error_total = sum(item["errors"] for item in _OP_STATS.values())
    op_total = sum(item["count"] for item in _OP_STATS.values()) or 1
    cache_hit_total = sum(metric["hits"] for metric in _DB_METRICS.values())
    cache_total = sum(metric["hits"] + metric["misses"] for metric in _DB_METRICS.values()) or 1
    return {
        "last_operations": list(_OP_HISTORY),
        "operations": operations,
        "summary": {
            "total_operations": op_total,
            "error_rate": round(error_total / op_total, 4),
            "average_cache_hit_rate": round(cache_hit_total / cache_total, 4),
        },
        "db": db_status(),
    }


def _record_db_metric(name: str, cache_hit: bool, elapsed_ms: float) -> None:
    metric = _DB_METRICS[name]
    metric["count"] += 1
    metric["hits" if cache_hit else "misses"] += 1
    logger.debug(
        "MONGO %s elapsed_ms=%.2f cache_hit=%s count=%s hits=%s misses=%s",
        name,
        elapsed_ms,
        cache_hit,
        metric["count"],
        metric["hits"],
        metric["misses"],
    )
    _DB_LAST_TIMINGS[name] = elapsed_ms


async def aget_setting(key: str, default: Any = None) -> Any:
    return await asyncio.to_thread(get_setting, key, default)


async def aget_bot(bot_name: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_bot, bot_name)


async def aget_bots() -> dict[str, dict[str, Any]]:
    return await asyncio.to_thread(get_bots)


async def alist_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_groups, enabled_only)


async def alist_messages(active_only: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_messages, active_only)


async def aset_setting(key: str, value: Any) -> bool:
    return await asyncio.to_thread(set_setting, key, value)


async def aset_bot_paused(bot_name: str, paused: bool) -> bool:
    return await asyncio.to_thread(set_bot_paused, bot_name, paused)


async def aupdate_group_runtime(*args: Any, **kwargs: Any) -> bool:
    return await asyncio.to_thread(update_group_runtime, *args, **kwargs)


def get_promotion_asset_channel() -> Any:
    return get_setting("promotion_asset_channel", None)


def get_promotion_sticker_message_id() -> Any:
    return get_setting("promotion_sticker_message_id", None)


def set_promotion_asset_channel(value: Any) -> bool:
    return set_setting("promotion_asset_channel", value)


def set_promotion_sticker_message_id(value: Any) -> bool:
    return set_setting("promotion_sticker_message_id", value)


def db_status() -> dict[str, Any]:
    return {
        "settings_cache": len(_SETTING_CACHE),
        "bots_cache": len(_BOT_CACHE),
        "groups_cache": len(_GROUP_CACHE),
        "messages_cache": len(_MESSAGE_CACHE),
        "metrics": {key: dict(value) for key, value in _DB_METRICS.items()},
        "last_timings_ms": dict(_DB_LAST_TIMINGS),
    }


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or os.getenv(name.lower(), default)


def _mongo_db():
    global _MONGO_CLIENT, _MONGO_DB
    if _MONGO_DB is not None:
        return _MONGO_DB
    uri = _env("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is required")
    logger.info("Connecting to MongoDB")
    _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=10000)
    _MONGO_CLIENT.admin.command("ping")
    _MONGO_DB = _MONGO_CLIENT["telegram_automation"]
    return _MONGO_DB


def init_db() -> None:
    db = _mongo_db()
    db["settings"].update_one({"_id": "promotion_mode"}, {"$setOnInsert": {"value": "message"}}, upsert=True)
    db["settings"].update_one({"_id": "promotion_asset_channel"}, {"$setOnInsert": {"value": None}}, upsert=True)
    db["settings"].update_one({"_id": "promotion_sticker_message_id"}, {"$setOnInsert": {"value": None}}, upsert=True)
    db["automation_analytics"].create_index([("scope", 1), ("period", 1)])
    db["automation_analytics"].create_index([("scope", 1), ("bot_name", 1)])
    db["automation_analytics"].create_index([("updated_at", 1)])
    db["groups"].create_index([("status", 1), ("updated_at", 1)])
    db["groups"].create_index([("status", 1), ("next_run_at", 1)])
    db["groups"].create_index([("status", 1), ("cooldown_until", 1)])
    db["messages"].create_index([("is_active", 1), ("id", 1)])


def _settings_value(doc: dict[str, Any] | None, default: Any) -> Any:
    if doc is None:
        return default
    return doc.get("value", default)


def set_setting(key: str, value: Any) -> bool:
    _mongo_db()["settings"].update_one({"_id": key}, {"$set": {"value": value, "updated_at": datetime.utcnow()}}, upsert=True)
    _SETTING_CACHE[key] = value
    return True


def get_setting(key: str, default: Any = None) -> Any:
    start = time.monotonic()
    if key in _SETTING_CACHE:
        value = _SETTING_CACHE[key]
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_setting", True, elapsed_ms)
        record_operation("get_setting", elapsed_ms, True, "mongo", {"cache_hit": True})
        return value
    doc = _mongo_db()["settings"].find_one({"_id": key})
    value = _settings_value(doc, default)
    _SETTING_CACHE[key] = value
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("get_setting", False, elapsed_ms)
    record_operation("get_setting", elapsed_ms, True, "mongo", {"cache_hit": False})
    return value


def delete_setting(key: str) -> bool:
    _mongo_db()["settings"].delete_one({"_id": key})
    _SETTING_CACHE.pop(key, None)
    return True


def _bots_collection():
    return _mongo_db()["bots"]


def add_bot(bot_name: str, config: dict[str, Any]) -> bool:
    _bots_collection().replace_one({"_id": bot_name}, {"_id": bot_name, **config, "updated_at": datetime.utcnow()}, upsert=True)
    _BOT_CACHE[bot_name] = dict(config)
    return True


def get_bot(bot_name: str) -> dict[str, Any] | None:
    start = time.monotonic()
    if bot_name in _BOT_CACHE:
        bot = dict(_BOT_CACHE[bot_name])
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_bot", True, elapsed_ms)
        record_operation("get_bot", elapsed_ms, True, "mongo", {"cache_hit": True})
        return bot
    doc = _bots_collection().find_one({"_id": bot_name})
    if not doc:
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_bot", False, elapsed_ms)
        record_operation("get_bot", elapsed_ms, False, "mongo", {"cache_hit": False})
        return None
    bot = {k: v for k, v in doc.items() if k != "_id"}
    _BOT_CACHE[bot_name] = dict(bot)
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("get_bot", False, elapsed_ms)
    record_operation("get_bot", elapsed_ms, True, "mongo", {"cache_hit": False})
    return bot


def replace_bot(bot_name: str, config: dict[str, Any]) -> bool:
    return add_bot(bot_name, config)


def update_bot(bot_name: str, **changes: Any) -> bool:
    current = get_bot(bot_name)
    if current is None:
        return False
    current.update(changes)
    return add_bot(bot_name, current)


def delete_bot(bot_name: str) -> bool:
    _bots_collection().delete_one({"_id": bot_name})
    _BOT_CACHE.pop(bot_name, None)
    delete_setting(f"bot_enabled_{bot_name}")
    delete_setting(f"bot_paused_{bot_name}")
    return True


def get_bots() -> dict[str, dict[str, Any]]:
    if _BOT_CACHE:
        return {name: dict(config) for name, config in sorted(_BOT_CACHE.items(), key=lambda item: item[0])}
    bots = {
        str(doc["_id"]): {k: v for k, v in doc.items() if k != "_id"}
        for doc in _bots_collection().find().sort("_id", 1)
    }
    _BOT_CACHE.update({name: dict(config) for name, config in bots.items()})
    return bots


def set_bot_enabled(bot_name: str, enabled: bool) -> bool:
    current = get_bot(bot_name) or {}
    current["enabled"] = enabled
    return add_bot(bot_name, current)


def set_all_bots_enabled(enabled: bool) -> int:
    count = 0
    for bot_name in get_bots().keys():
        if set_bot_enabled(bot_name, enabled):
            if not enabled:
                set_bot_paused(bot_name, False)
            count += 1
    return count


def is_bot_enabled(bot_name: str, default: bool = False) -> bool:
    bot = get_bot(bot_name) or {}
    if "enabled" in bot:
        return bool(bot["enabled"])
    return bool(get_setting(f"bot_enabled_{bot_name}", default))


def set_bot_paused(bot_name: str, paused: bool) -> bool:
    return set_setting(f"bot_paused_{bot_name}", paused)


def is_bot_paused(bot_name: str, default: bool = False) -> bool:
    return bool(get_setting(f"bot_paused_{bot_name}", default))


def _groups_collection():
    return _mongo_db()["groups"]


def add_group(group_id: str, group_name: str, status: str = "enabled") -> bool:
    now = datetime.utcnow()
    payload = {
        "_id": str(group_id),
        "group_id": str(group_id),
        "delay_min": 4,
        "delay_max": 7,
        "special_message": None,
        "last_status": None,
        "last_error": None,
        "fail_count": 0,
        "last_failed_at": None,
        "cooldown_until": None,
        "next_run_at": None,
        "last_sent_at": None,
        "active_start_hour": None,
        "active_end_hour": None,
    }
    _groups_collection().update_one(
        {"_id": str(group_id)},
        {
            "$setOnInsert": payload,
            "$set": {
                "group_name": group_name,
                "status": status,
                "updated_at": now,
            },
        },
        upsert=True,
    )
    _GROUP_CACHE[str(group_id)] = {k: v for k, v in payload.items() if k != "_id"}
    _GROUP_CACHE[str(group_id)]["group_name"] = group_name
    _GROUP_CACHE[str(group_id)]["status"] = status
    _GROUP_CACHE[str(group_id)]["updated_at"] = now
    return True


def get_group(group_id: str) -> dict[str, Any] | None:
    cache_key = str(group_id)
    if cache_key in _GROUP_CACHE:
        return dict(_GROUP_CACHE[cache_key])
    doc = _groups_collection().find_one({"_id": str(group_id)})
    if not doc:
        return None
    group = {k: v for k, v in doc.items() if k != "_id"}
    group.setdefault("group_id", str(group_id))
    _GROUP_CACHE[cache_key] = dict(group)
    return group


def list_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    start = time.monotonic()
    if _GROUP_CACHE:
        groups = [dict(group) for group in _GROUP_CACHE.values()]
        filtered = [group for group in groups if (group.get("status") == "enabled")] if enabled_only else groups
        result = sorted(filtered, key=lambda item: item.get("updated_at") or datetime.min)
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("list_groups", True, elapsed_ms)
        record_operation("list_groups", elapsed_ms, True, "mongo", {"cache_hit": True, "enabled_only": enabled_only})
        return result
    query = {"status": "enabled"} if enabled_only else {}
    groups = [{k: v for k, v in doc.items() if k != "_id"} for doc in _groups_collection().find(query).sort("updated_at", 1)]
    for group in groups:
        group_id = str(group.get("group_id"))
        if group_id:
            _GROUP_CACHE[group_id] = dict(group)
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("list_groups", False, elapsed_ms)
    record_operation("list_groups", elapsed_ms, True, "mongo", {"cache_hit": False, "enabled_only": enabled_only})
    return groups


def delete_group(group_id: str) -> bool:
    _groups_collection().delete_one({"_id": str(group_id)})
    _GROUP_CACHE.pop(str(group_id), None)
    return True


def set_group_status(group_id: str, status: str) -> bool:
    now = datetime.utcnow()
    _groups_collection().update_one({"_id": str(group_id)}, {"$set": {"status": status, "updated_at": now}})
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["status"] = status
        cached["updated_at"] = now
    return True


def set_all_groups_status(status: str) -> int:
    count = 0
    for group in list_groups():
        if set_group_status(str(group.get("group_id")), status):
            count += 1
    return count


def update_group_runtime(group_id: str, last_status: str | None = None, last_error: str | None = None, fail_count: int | None = None, last_failed_at: str | None = None, cooldown_until: str | None | object = None, next_run_at: str | None | object = None, last_sent_at: str | None | object = None) -> bool:
    update: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if last_status is not None:
        update["last_status"] = last_status
    if last_error is not None:
        update["last_error"] = last_error
    if fail_count is not None:
        update["fail_count"] = int(fail_count)
    if last_failed_at is not None:
        update["last_failed_at"] = last_failed_at
    if cooldown_until is not None:
        update["cooldown_until"] = cooldown_until
    if next_run_at is not None:
        update["next_run_at"] = next_run_at
    if last_sent_at is not None:
        update["last_sent_at"] = last_sent_at
    _groups_collection().update_one({"_id": str(group_id)}, {"$set": update})
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached.update(update)
    return True


def update_group_name(group_id: str, group_name: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["group_name"] = group_name
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"group_name": group_name, "updated_at": datetime.utcnow()}}))


def update_group_delay(group_id: str, delay_min: int, delay_max: int) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["delay_min"] = delay_min
        cached["delay_max"] = delay_max
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"delay_min": delay_min, "delay_max": delay_max, "updated_at": datetime.utcnow()}}))


def update_group_time_window(group_id: str, active_start_hour: int | None, active_end_hour: int | None) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["active_start_hour"] = active_start_hour
        cached["active_end_hour"] = active_end_hour
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"active_start_hour": active_start_hour, "active_end_hour": active_end_hour, "updated_at": datetime.utcnow()}}))


def set_group_special_message(group_id: str, message: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["special_message"] = message
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": message, "updated_at": datetime.utcnow()}}))


def clear_group_special_message(group_id: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["special_message"] = None
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": None, "updated_at": datetime.utcnow()}}))


def _messages_collection():
    return _mongo_db()["messages"]


def _next_message_id() -> int:
    doc = _messages_collection().find_one(sort=[("id", -1)])
    return int(doc["id"]) + 1 if doc and "id" in doc else 1


def add_message(content: str, delay_minutes: int, media_type: str | None = None, media_file_id: str | None = None) -> int:
    message_id = _next_message_id()
    doc = {"id": message_id, "content": content, "media_type": media_type, "media_file_id": media_file_id, "delay_minutes": delay_minutes, "is_active": True, "created_at": datetime.utcnow()}
    _messages_collection().insert_one(doc)
    _MESSAGE_CACHE[message_id] = dict(doc)
    return message_id


def get_message(message_id: int) -> dict[str, Any] | None:
    if message_id in _MESSAGE_CACHE:
        return dict(_MESSAGE_CACHE[message_id])
    doc = _messages_collection().find_one({"id": int(message_id)})
    if not doc:
        return None
    message = {k: v for k, v in doc.items() if k != "_id"}
    _MESSAGE_CACHE[message_id] = dict(message)
    return message


def list_messages(active_only: bool = True) -> list[dict[str, Any]]:
    start = time.monotonic()
    if _MESSAGE_CACHE:
        messages = [dict(message) for message in _MESSAGE_CACHE.values()]
        filtered = [message for message in messages if message.get("is_active", False)] if active_only else messages
        result = sorted(filtered, key=lambda item: item.get("id", 0))
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("list_messages", True, elapsed_ms)
        record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": True, "active_only": active_only})
        return result
    query = {"is_active": True} if active_only else {}
    messages = [{k: v for k, v in doc.items() if k != "_id"} for doc in _messages_collection().find(query).sort("id", 1)]
    for message in messages:
        message_id = int(message.get("id"))
        _MESSAGE_CACHE[message_id] = dict(message)
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("list_messages", False, elapsed_ms)
    record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": False, "active_only": active_only})
    return messages


def delete_message(message_id: int) -> bool:
    _messages_collection().delete_one({"id": int(message_id)})
    _MESSAGE_CACHE.pop(int(message_id), None)
    return True
