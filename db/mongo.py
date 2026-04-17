import os
import logging
from pymongo import MongoClient
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    logger.warning("MONGO_URI not set, MongoDB will not be used")
    client = None
    db = None
else:
    try:
        client = MongoClient(MONGO_URI)
        db = client["telegram_automation"]
        client.admin.command('ping')
        logger.info("Connected to MongoDB")
    except PyMongoError as e:
        logger.error(f"MongoDB connection failed: {e}")
        client = None
        db = None

bots_collection = db["bots"] if db else None
groups_collection = db["groups"] if db else None
settings_collection = db["settings"] if db else None


def add_bot(bot_name: str, config: dict) -> bool:
    if not bots_collection:
        return False
    try:
        bots_collection.update_one(
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
    if not bots_collection:
        return {}
    try:
        return {bot["_id"]: bot for bot in bots_collection.find()}
    except PyMongoError as e:
        logger.error(f"MongoDB get_bots error: {e}")
        return {}


def delete_bot(bot_name: str) -> bool:
    if not bots_collection:
        return False
    try:
        bots_collection.delete_one({"_id": bot_name})
        logger.info(f"MongoDB: Deleted bot: {bot_name}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB delete_bot error: {e}")
        return False


def add_group(group_id: str) -> bool:
    if not groups_collection:
        return False
    try:
        groups_collection.update_one(
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
    if not groups_collection:
        return []
    try:
        return [g["_id"] for g in groups_collection.find()]
    except PyMongoError as e:
        logger.error(f"MongoDB get_groups error: {e}")
        return []


def delete_group(group_id: str) -> bool:
    if not groups_collection:
        return False
    try:
        groups_collection.delete_one({"_id": group_id})
        logger.info(f"MongoDB: Deleted group: {group_id}")
        return True
    except PyMongoError as e:
        logger.error(f"MongoDB delete_group error: {e}")
        return False


def set_setting(key: str, value) -> bool:
    if not settings_collection:
        return False
    try:
        settings_collection.update_one(
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
    if not settings_collection:
        return default
    try:
        doc = settings_collection.find_one({"_id": key})
        return doc["value"] if doc else default
    except PyMongoError as e:
        logger.error(f"MongoDB get_setting error: {e}")
        return default