import json
import logging
import os
from datetime import datetime
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
logger = logging.getLogger(__name__)

_MONGO_CLIENT: MongoClient | None = None
_MONGO_DB = None


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
    db["settings"].update_one({"_id": "promotion_sticker"}, {"$setOnInsert": {"value": None}}, upsert=True)
    db["settings"].update_one({"_id": "promotion_sticker_path"}, {"$setOnInsert": {"value": None}}, upsert=True)


def _settings_value(doc: dict[str, Any] | None, default: Any) -> Any:
    if doc is None:
        return default
    return doc.get("value", default)


def set_setting(key: str, value: Any) -> bool:
    _mongo_db()["settings"].update_one({"_id": key}, {"$set": {"value": value, "updated_at": datetime.utcnow()}}, upsert=True)
    return True


def get_setting(key: str, default: Any = None) -> Any:
    doc = _mongo_db()["settings"].find_one({"_id": key})
    return _settings_value(doc, default)


def delete_setting(key: str) -> bool:
    _mongo_db()["settings"].delete_one({"_id": key})
    return True


def _bots_collection():
    return _mongo_db()["bots"]


def add_bot(bot_name: str, config: dict[str, Any]) -> bool:
    _bots_collection().replace_one({"_id": bot_name}, {"_id": bot_name, **config, "updated_at": datetime.utcnow()}, upsert=True)
    return True


def get_bot(bot_name: str) -> dict[str, Any] | None:
    doc = _bots_collection().find_one({"_id": bot_name})
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id"}


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
    delete_setting(f"bot_enabled_{bot_name}")
    delete_setting(f"bot_paused_{bot_name}")
    return True


def get_bots() -> dict[str, dict[str, Any]]:
    return {
        str(doc["_id"]): {k: v for k, v in doc.items() if k != "_id"}
        for doc in _bots_collection().find().sort("_id", 1)
    }


def set_bot_enabled(bot_name: str, enabled: bool) -> bool:
    current = get_bot(bot_name) or {}
    current["enabled"] = enabled
    return add_bot(bot_name, current)


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
    payload = {
        "_id": str(group_id),
        "group_id": str(group_id),
        "group_name": group_name,
        "status": status,
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
        "updated_at": datetime.utcnow(),
    }
    _groups_collection().update_one({"_id": str(group_id)}, {"$setOnInsert": payload, "$set": {"group_name": group_name, "status": status, "updated_at": datetime.utcnow()}}, upsert=True)
    return True


def get_group(group_id: str) -> dict[str, Any] | None:
    doc = _groups_collection().find_one({"_id": str(group_id)})
    if not doc:
        return None
    group = {k: v for k, v in doc.items() if k != "_id"}
    group.setdefault("group_id", str(group_id))
    return group


def list_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    query = {"status": "enabled"} if enabled_only else {}
    return [{k: v for k, v in doc.items() if k != "_id"} for doc in _groups_collection().find(query).sort("updated_at", 1)]


def delete_group(group_id: str) -> bool:
    _groups_collection().delete_one({"_id": str(group_id)})
    return True


def set_group_status(group_id: str, status: str) -> bool:
    _groups_collection().update_one({"_id": str(group_id)}, {"$set": {"status": status, "updated_at": datetime.utcnow()}})
    return True


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
    return True


def update_group_name(group_id: str, group_name: str) -> bool:
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"group_name": group_name, "updated_at": datetime.utcnow()}}))


def update_group_delay(group_id: str, delay_min: int, delay_max: int) -> bool:
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"delay_min": delay_min, "delay_max": delay_max, "updated_at": datetime.utcnow()}}))


def update_group_time_window(group_id: str, active_start_hour: int | None, active_end_hour: int | None) -> bool:
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"active_start_hour": active_start_hour, "active_end_hour": active_end_hour, "updated_at": datetime.utcnow()}}))


def set_group_special_message(group_id: str, message: str) -> bool:
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": message, "updated_at": datetime.utcnow()}}))


def clear_group_special_message(group_id: str) -> bool:
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": None, "updated_at": datetime.utcnow()}}))


def _messages_collection():
    return _mongo_db()["messages"]


def _next_message_id() -> int:
    doc = _messages_collection().find_one(sort=[("id", -1)])
    return int(doc["id"]) + 1 if doc and "id" in doc else 1


def add_message(content: str, delay_minutes: int, media_type: str | None = None, media_file_id: str | None = None) -> int:
    message_id = _next_message_id()
    _messages_collection().insert_one({"id": message_id, "content": content, "media_type": media_type, "media_file_id": media_file_id, "delay_minutes": delay_minutes, "is_active": True, "created_at": datetime.utcnow()})
    return message_id


def get_message(message_id: int) -> dict[str, Any] | None:
    doc = _messages_collection().find_one({"id": int(message_id)})
    return None if not doc else {k: v for k, v in doc.items() if k != "_id"}


def list_messages(active_only: bool = True) -> list[dict[str, Any]]:
    query = {"is_active": True} if active_only else {}
    return [{k: v for k, v in doc.items() if k != "_id"} for doc in _messages_collection().find(query).sort("id", 1)]


def delete_message(message_id: int) -> bool:
    _messages_collection().delete_one({"id": int(message_id)})
    return True

