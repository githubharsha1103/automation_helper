import os
import logging
from pymongo import MongoClient
from pymongo.errors import PyMongoError

print("📦 Loading db module...")

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
print("📦 MONGO_URI:", MONGO_URI)

_client = None
_db = None


def get_db():
    global _client, _db
    if _db is not None:
        return _db
    
    if not MONGO_URI:
        logger.warning("MONGO_URI not set, MongoDB will not be used")
        return None
    
    try:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = _client["telegram_automation"]
        _client.admin.command('ping')
        logger.info("Connected to MongoDB")
        print("📦 MongoDB Connected!")
        return _db
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        print(f"📦 MongoDB connection error: {e}")
        _client = None
        _db = None
        return None


def get_collections():
    database = get_db()
    if database is None:
        return {"bots": None, "groups": None, "settings": None}
    return {
        "bots": database["bots"],
        "groups": database["groups"],
        "settings": database["settings"]
    }


def add_bot(bot_name: str, config: dict) -> bool:
    cols = get_collections()
    bots_coll = cols.get("bots")
    if bots_coll is None:
        return False
    try:
        bots_coll.update_one(
            {"_id": bot_name},
            {"$set": config},
            upsert=True
        )
        logger.info(f"MongoDB: Added/updated bot: {bot_name}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB add_bot error: {e}")
        return False


def get_bots() -> dict:
    cols = get_collections()
    bots_coll = cols.get("bots")
    if bots_coll is None:
        return {}
    try:
        return {bot["_id"]: bot for bot in bots_coll.find()}
    except PyMongoError as e:
        logger.error(f"MongoDB get_bots error: {e}")
        return {}


def delete_bot(bot_name: str) -> bool:
    cols = get_collections()
    bots_coll = cols.get("bots")
    if bots_coll is None:
        return False
    try:
        bots_coll.delete_one({"_id": bot_name})
        logger.info(f"MongoDB: Deleted bot: {bot_name}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB delete_bot error: {e}")
        return False


def add_group(group_id: str) -> bool:
    cols = get_collections()
    groups_coll = cols.get("groups")
    if groups_coll is None:
        return False
    try:
        groups_coll.update_one(
            {"_id": group_id},
            {"$set": {"group_id": group_id}},
            upsert=True
        )
        logger.info(f"MongoDB: Added group: {group_id}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB add_group error: {e}")
        return False


def get_groups() -> list:
    cols = get_collections()
    groups_coll = cols.get("groups")
    if groups_coll is None:
        return []
    try:
        return [g["_id"] for g in groups_coll.find()]
    except PyMongoError as e:
        logger.error(f"MongoDB get_groups error: {e}")
        return []


def delete_group(group_id: str) -> bool:
    cols = get_collections()
    groups_coll = cols.get("groups")
    if groups_coll is None:
        return False
    try:
        groups_coll.delete_one({"_id": group_id})
        logger.info(f"MongoDB: Deleted group: {group_id}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB delete_group error: {e}")
        return False


def set_setting(key: str, value) -> bool:
    cols = get_collections()
    settings_coll = cols.get("settings")
    if settings_coll is None:
        return False
    try:
        settings_coll.update_one(
            {"_id": key},
            {"$set": {"value": value}},
            upsert=True
        )
        logger.info(f"MongoDB: Set setting: {key}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB set_setting error: {e}")
        return False


def get_setting(key: str, default=None):
    cols = get_collections()
    settings_coll = cols.get("settings")
    if settings_coll is None:
        return default
    try:
        doc = settings_coll.find_one({"_id": key})
        return doc["value"] if doc else default
    except PyMongoError as e:
        logger.error(f"MongoDB get_setting error: {e}")
        return default