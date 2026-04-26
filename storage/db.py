import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "telegram_auto_reply.db"
_LOCK = threading.RLock()
_MONGO_CLIENT = None
_MONGO_DB = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or os.getenv(name.lower(), default)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _get_mongo_db():
    global _MONGO_CLIENT, _MONGO_DB
    if _MONGO_DB is not None:
        return _MONGO_DB

    uri = _env("MONGO_URI")
    if not uri:
        return None

    try:
        _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=10000)
        _MONGO_CLIENT.admin.command("ping")
        _MONGO_DB = _MONGO_CLIENT["telegram_automation"]
        return _MONGO_DB
    except Exception:
        _MONGO_CLIENT = None
        _MONGO_DB = None
        return None


def init_db() -> None:
    with _LOCK:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_name TEXT UNIQUE NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT UNIQUE NOT NULL,
                    group_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'enabled',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    media_type TEXT,
                    media_file_id TEXT,
                    delay_minutes INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()
            }
            if "group_name" not in columns:
                if "name" in columns:
                    conn.execute("ALTER TABLE groups RENAME COLUMN name TO group_name")
                else:
                    conn.execute("ALTER TABLE groups ADD COLUMN group_name TEXT")
                    conn.execute("UPDATE groups SET group_name = COALESCE(group_name, group_id)")
            conn.commit()


def _sqlite_fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _LOCK:
        with _connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]


def _sqlite_fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None


def _sqlite_execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount


def _sync_group_to_sqlite(group: dict[str, Any]) -> None:
    _sqlite_execute(
        """
        INSERT INTO groups (group_id, group_name, status)
        VALUES (?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            group_name=excluded.group_name,
            status=excluded.status,
            updated_at=CURRENT_TIMESTAMP
        """,
        (str(group["group_id"]), group["group_name"], group["status"]),
    )


def _sync_message_to_sqlite(message: dict[str, Any]) -> None:
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO messages
                (id, content, media_type, media_file_id, delay_minutes, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    message["content"],
                    message.get("media_type"),
                    message.get("media_file_id"),
                    message["delay_minutes"],
                    1 if message.get("is_active", True) else 0,
                ),
            )
            conn.commit()


def _sync_bot_to_sqlite(bot_name: str, config: dict[str, Any]) -> None:
    _sqlite_execute(
        """
        INSERT INTO bots (bot_name, config_json)
        VALUES (?, ?)
        ON CONFLICT(bot_name) DO UPDATE SET config_json=excluded.config_json
        """,
        (bot_name, json.dumps(config)),
    )


def add_group(group_id: str, group_name: str, status: str = "enabled") -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        now = datetime.utcnow()
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {
                "$set": {
                    "group_id": normalized_id,
                    "group_name": group_name,
                    "enabled": status == "enabled",
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "delay_min": 4,
                    "delay_max": 7,
                    "created_at": now,
                    "last_error": None,
                },
            },
            upsert=True,
        )
    _sync_group_to_sqlite(
        {"group_id": normalized_id, "group_name": group_name, "status": status}
    )
    return True


def get_group(group_id: str) -> dict[str, Any] | None:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        doc = mongo["groups"].find_one({"_id": normalized_id})
        if doc:
            group = {
                "group_id": normalized_id,
                "group_name": doc.get("group_name") or doc.get("name") or normalized_id,
                "status": "enabled" if doc.get("enabled", True) else "disabled",
                "delay_min": doc.get("delay_min", 4),
                "delay_max": doc.get("delay_max", 7),
                "special_message": doc.get("special_message"),
                "last_status": doc.get("last_status") or doc.get("last_run_status") or "N/A",
                "last_error": doc.get("last_error"),
            }
            _sync_group_to_sqlite(group)
            return group
    return _sqlite_fetch_one("SELECT * FROM groups WHERE group_id = ?", (normalized_id,))


def list_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    mongo = _get_mongo_db()
    if mongo is not None:
        query = {"enabled": True} if enabled_only else {}
        groups = []
        for doc in mongo["groups"].find(query).sort("created_at", 1):
            group = {
                "group_id": str(doc.get("group_id", doc["_id"])),
                "group_name": doc.get("group_name") or doc.get("name") or str(doc["_id"]),
                "status": "enabled" if doc.get("enabled", True) else "disabled",
                "delay_min": doc.get("delay_min", 4),
                "delay_max": doc.get("delay_max", 7),
                "special_message": doc.get("special_message"),
                "last_status": doc.get("last_status") or doc.get("last_run_status") or "N/A",
                "last_error": doc.get("last_error"),
            }
            groups.append(group)
            _sync_group_to_sqlite(group)
        return groups

    if enabled_only:
        return _sqlite_fetch_all("SELECT * FROM groups WHERE status = 'enabled' ORDER BY id ASC")
    return _sqlite_fetch_all("SELECT * FROM groups ORDER BY id ASC")


def delete_group(group_id: str) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].delete_one({"_id": normalized_id})
    return _sqlite_execute("DELETE FROM groups WHERE group_id = ?", (normalized_id,)) >= 0


def set_group_status(group_id: str, status: str) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {"$set": {"enabled": status == "enabled", "updated_at": datetime.utcnow()}},
        )
    return (
        _sqlite_execute(
            "UPDATE groups SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
            (status, normalized_id),
        )
        >= 0
    )


def update_group_runtime(group_id: str, last_status: str | None = None, last_error: str | None = None) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    update_fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if last_status is not None:
        update_fields["last_status"] = last_status
        update_fields["last_run_status"] = last_status
    if last_error is not None:
        update_fields["last_error"] = last_error
    if mongo is not None:
        mongo["groups"].update_one({"_id": normalized_id}, {"$set": update_fields})
    return True


def update_group_name(group_id: str, group_name: str) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {"$set": {"group_name": group_name, "updated_at": datetime.utcnow()}},
        )
    return (
        _sqlite_execute(
            "UPDATE groups SET group_name = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
            (group_name, normalized_id),
        )
        >= 0
    )


def update_group_delay(group_id: str, delay_min: int, delay_max: int) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {
                "$set": {
                    "delay_min": delay_min,
                    "delay_max": delay_max,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
    return True


def set_group_special_message(group_id: str, message: str) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {"$set": {"special_message": message, "updated_at": datetime.utcnow()}},
        )
    return True


def clear_group_special_message(group_id: str) -> bool:
    mongo = _get_mongo_db()
    normalized_id = str(group_id)
    if mongo is not None:
        mongo["groups"].update_one(
            {"_id": normalized_id},
            {"$set": {"special_message": None, "updated_at": datetime.utcnow()}},
        )
    return True


def add_message(
    content: str,
    delay_minutes: int,
    media_type: str | None = None,
    media_file_id: str | None = None,
) -> int:
    mongo = _get_mongo_db()
    if mongo is not None:
        doc = {
            "text": content,
            "delay_minutes": delay_minutes,
            "media_type": media_type,
            "media_file_id": media_file_id,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = mongo["messages_collection"].insert_one(doc)
        message_id = str(result.inserted_id)
    else:
        with _LOCK:
            with _connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO messages (content, media_type, media_file_id, delay_minutes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (content, media_type, media_file_id, delay_minutes),
                )
                conn.commit()
                return int(cursor.lastrowid)

    existing = list_messages(active_only=False)
    max_id = max((int(item["id"]) for item in existing if str(item["id"]).isdigit()), default=0) + 1
    _sync_message_to_sqlite(
        {
            "id": max_id,
            "content": content,
            "media_type": media_type,
            "media_file_id": media_file_id,
            "delay_minutes": delay_minutes,
            "is_active": True,
        }
    )
    return max_id


def _mongo_messages() -> list[dict[str, Any]]:
    mongo = _get_mongo_db()
    if mongo is None:
        return []
    messages: list[dict[str, Any]] = []
    docs = []
    seen_ids: set[str] = set()
    for collection_name in ["messages_collection", "bot_messages", "group_messages"]:
        for doc in mongo[collection_name].find().sort("created_at", 1):
            mongo_id = str(doc["_id"])
            if mongo_id in seen_ids:
                continue
            seen_ids.add(mongo_id)
            doc["_source_collection"] = collection_name
            docs.append(doc)

    for index, doc in enumerate(docs, start=1):
        message = {
            "id": index,
            "mongo_id": str(doc["_id"]),
            "mongo_collection": doc.get("_source_collection", "messages_collection"),
            "content": doc.get("text") or doc.get("content") or "",
            "media_type": doc.get("media_type"),
            "media_file_id": doc.get("media_file_id"),
            "delay_minutes": int(doc.get("delay_minutes", 1) or 1),
            "is_active": True,
        }
        messages.append(message)
        _sync_message_to_sqlite(message)
    return messages


def get_message(message_id: int) -> dict[str, Any] | None:
    for message in list_messages(active_only=False):
        if int(message["id"]) == int(message_id):
            return message
    return None


def list_messages(active_only: bool = True) -> list[dict[str, Any]]:
    mongo_messages = _mongo_messages()
    if mongo_messages:
        return mongo_messages

    if active_only:
        return _sqlite_fetch_all("SELECT * FROM messages WHERE is_active = 1 ORDER BY id ASC")
    return _sqlite_fetch_all("SELECT * FROM messages ORDER BY id ASC")


def delete_message(message_id: int) -> bool:
    mongo = _get_mongo_db()
    messages = list_messages(active_only=False)
    target = next((item for item in messages if int(item["id"]) == int(message_id)), None)
    if mongo is not None and target and target.get("mongo_id"):
        object_id = ObjectId(target["mongo_id"])
        collection_name = target.get("mongo_collection")
        if collection_name in {"messages_collection", "bot_messages", "group_messages"}:
            mongo[collection_name].delete_one({"_id": object_id})
        else:
            mongo["messages_collection"].delete_one({"_id": object_id})
            mongo["bot_messages"].delete_one({"_id": object_id})
            mongo["group_messages"].delete_one({"_id": object_id})
    return _sqlite_execute("DELETE FROM messages WHERE id = ?", (message_id,)) >= 0


def set_setting(key: str, value: Any) -> bool:
    mongo = _get_mongo_db()
    if mongo is not None:
        mongo["settings"].update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)
    serialized = json.dumps(value)
    _sqlite_execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, serialized),
    )
    return True


def delete_setting(key: str) -> bool:
    mongo = _get_mongo_db()
    if mongo is not None:
        mongo["settings"].delete_one({"_id": key})
    return _sqlite_execute("DELETE FROM settings WHERE key = ?", (key,)) >= 0


def get_setting(key: str, default: Any = None) -> Any:
    mongo = _get_mongo_db()
    if mongo is not None:
        doc = mongo["settings"].find_one({"_id": key})
        if doc is not None:
            return doc.get("value", default)
    row = _sqlite_fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def add_bot(bot_name: str, config: dict[str, Any]) -> bool:
    mongo = _get_mongo_db()
    if mongo is not None:
        payload = {"_id": bot_name, **config}
        mongo["bots"].replace_one({"_id": bot_name}, payload, upsert=True)
    _sync_bot_to_sqlite(bot_name, config)
    return True


def get_bot(bot_name: str) -> dict[str, Any] | None:
    mongo = _get_mongo_db()
    if mongo is not None:
        doc = mongo["bots"].find_one({"_id": bot_name})
        if doc:
            config = {k: v for k, v in doc.items() if k != "_id"}
            _sync_bot_to_sqlite(bot_name, config)
            return config
        return None
    row = _sqlite_fetch_one("SELECT config_json FROM bots WHERE bot_name = ?", (bot_name,))
    if not row:
        return None
    return json.loads(row["config_json"])


def replace_bot(bot_name: str, config: dict[str, Any]) -> bool:
    return add_bot(bot_name, config)


def update_bot(bot_name: str, **changes: Any) -> bool:
    current = get_bot(bot_name)
    if current is None:
        return False
    current.update(changes)
    return replace_bot(bot_name, current)


def set_bot_enabled(bot_name: str, enabled: bool) -> bool:
    current = get_bot(bot_name)
    if current is not None:
        current["enabled"] = enabled
        replace_bot(bot_name, current)
    return set_setting(f"bot_enabled_{bot_name}", enabled)


def is_bot_enabled(bot_name: str, default: bool = False) -> bool:
    bot = get_bot(bot_name) or {}
    if "enabled" in bot:
        return bool(bot["enabled"])
    return bool(get_setting(f"bot_enabled_{bot_name}", default))


def set_bot_paused(bot_name: str, paused: bool) -> bool:
    return set_setting(f"bot_paused_{bot_name}", paused)


def is_bot_paused(bot_name: str, default: bool = False) -> bool:
    return bool(get_setting(f"bot_paused_{bot_name}", default))


def get_bots() -> dict[str, dict[str, Any]]:
    mongo = _get_mongo_db()
    if mongo is not None:
        bots: dict[str, dict[str, Any]] = {}
        for doc in mongo["bots"].find().sort("_id", 1):
            bot_name = str(doc["_id"])
            config = {k: v for k, v in doc.items() if k != "_id"}
            bots[bot_name] = config
            _sync_bot_to_sqlite(bot_name, config)
        return bots

    rows = _sqlite_fetch_all("SELECT bot_name, config_json FROM bots ORDER BY id ASC")
    bots: dict[str, dict[str, Any]] = {}
    for row in rows:
        bots[row["bot_name"]] = json.loads(row["config_json"])
    return bots


def delete_bot(bot_name: str) -> bool:
    mongo = _get_mongo_db()
    if mongo is not None:
        mongo["bots"].delete_one({"_id": bot_name})
    delete_setting(f"bot_enabled_{bot_name}")
    delete_setting(f"bot_paused_{bot_name}")
    return _sqlite_execute("DELETE FROM bots WHERE bot_name = ?", (bot_name,)) >= 0


init_db()
