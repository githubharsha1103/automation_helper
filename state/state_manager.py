import logging
import threading
import json
import os
import time
from config.bots_config import bots, default_limit

try:
    from db.mongo import add_bot as mongo_add_bot, get_bots as mongo_get_bots, delete_bot as mongo_delete_bot
    from db.mongo import add_group as mongo_add_group, get_groups as mongo_get_groups, delete_group as mongo_delete_group
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False
    mongo_add_bot = None
    mongo_get_bots = None
    mongo_delete_bot = None
    mongo_add_group = None
    mongo_get_groups = None
    mongo_delete_group = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._groups = []
        self._group_enabled = False
        self._dynamic_bots = {}
        self._group_index = 0
        self._group_last_sent = {}
        self._message_index = 0
        self._daily_count = 0
        self._daily_reset_time = time.time()
        self._group_settings = {
            "enabled": False,
            "max_groups_per_cycle": 5,
            "delay_range": (30, 90),
            "safe_mode": True
        }
        self.load_bots()
        self._initialize_state()
        logger.info("StateManager initialized")

    def load_bots(self):
        if MONGO_AVAILABLE and mongo_get_bots:
            mongo_bots = mongo_get_bots()
            if mongo_bots:
                self._dynamic_bots = {k: v for k, v in mongo_bots.items() if not k.startswith('_')}
                logger.info(f"MongoDB: Loaded {len(self._dynamic_bots)} dynamic bots")
                for bot_name in self._dynamic_bots.keys():
                    if bot_name not in self._state:
                        self._state[bot_name] = {
                            "enabled": False,
                            "limit": default_limit,
                            "count": 0,
                            "security_pause": False
                        }
                return
        try:
            with open("dynamic_bots.json", "r") as f:
                self._dynamic_bots = json.load(f)
                logger.info(f"Loaded {len(self._dynamic_bots)} dynamic bots from file")
                for bot_name in self._dynamic_bots.keys():
                    if bot_name not in self._state:
                        self._state[bot_name] = {
                            "enabled": False,
                            "limit": default_limit,
                            "count": 0,
                            "security_pause": False
                        }
        except:
            self._dynamic_bots = {}

    def save_bots(self):
        if MONGO_AVAILABLE and mongo_add_bot:
            for bot_name, config in self._dynamic_bots.items():
                mongo_add_bot(bot_name, config)
            logger.info("MongoDB: Saved dynamic bots")
        else:
            temp_file = "dynamic_bots.tmp"
            with open(temp_file, "w") as f:
                json.dump(self._dynamic_bots, f)
            os.replace(temp_file, "dynamic_bots.json")
            logger.info("Saved dynamic bots to file")

    def _initialize_state(self):
        for bot_name in bots.keys():
            self._state[bot_name] = {
                "enabled": False,
                "limit": default_limit,
                "count": 0,
                "security_pause": False
            }
        for bot_name in self._dynamic_bots.keys():
            if bot_name not in self._state:
                self._state[bot_name] = {
                    "enabled": False,
                    "limit": default_limit,
                    "count": 0,
                    "security_pause": False
                }

    def enable_bot(self, bot_name: str) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["enabled"] = True
                logger.info(f"Enabled bot: {bot_name}")
                return True
            logger.warning(f"Bot not found: {bot_name}")
            return False

    def disable_bot(self, bot_name: str) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["enabled"] = False
                logger.info(f"Disabled bot: {bot_name}")
                return True
            logger.warning(f"Bot not found: {bot_name}")
            return False

    def set_limit(self, bot_name: str, value: int) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["limit"] = value
                logger.info(f"Set limit for {bot_name}: {value}")
                return True
            logger.warning(f"Bot not found: {bot_name}")
            return False

    def increment_count(self, bot_name: str) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["count"] += 1
                return True
            return False

    def reset_count(self, bot_name: str) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["count"] = 0
                logger.info(f"Reset count for: {bot_name}")
                return True
            return False

    def get_state(self, bot_name: str) -> dict:
        with self._lock:
            return self._state.get(bot_name, {}).copy()

    def get_all_state(self) -> dict:
        with self._lock:
            return {k: v.copy() for k, v in self._state.items()}

    def is_enabled(self, bot_name: str) -> bool:
        with self._lock:
            return self._state.get(bot_name, {}).get("enabled", False)

    def should_stop(self, bot_name: str) -> bool:
        with self._lock:
            state = self._state.get(bot_name, {})
            return state.get("count", 0) >= state.get("limit", 0)

    def set_security_pause(self, bot_name: str, value: bool) -> bool:
        with self._lock:
            if bot_name in self._state:
                self._state[bot_name]["security_pause"] = value
                logger.info(f"Set security_pause for {bot_name}: {value}")
                return True
            logger.warning(f"Bot not found: {bot_name}")
            return False

    def is_security_paused(self, bot_name: str) -> bool:
        with self._lock:
            return self._state.get(bot_name, {}).get("security_pause", False)

    def add_group(self, group_id: str) -> bool:
        with self._lock:
            if group_id not in self._groups:
                self._groups.append(group_id)
                if MONGO_AVAILABLE and mongo_add_group:
                    mongo_add_group(group_id)
                logger.info(f"Added group: {group_id}")
                return True
            return False

    def remove_group(self, group_id: str) -> bool:
        with self._lock:
            if group_id in self._groups:
                self._groups.remove(group_id)
                if MONGO_AVAILABLE and mongo_delete_group:
                    mongo_delete_group(group_id)
                logger.info(f"Removed group: {group_id}")
                return True
            return False

    def get_groups(self) -> list:
        with self._lock:
            return self._groups.copy()

    def get_rotated_groups(self, max_groups: int = 5) -> list:
        with self._lock:
            if not self._groups:
                return []
            groups = self._groups.copy()
            if len(groups) <= max_groups:
                return groups
            rotated = []
            for i in range(max_groups):
                idx = (self._group_index + i) % len(groups)
                rotated.append(groups[idx])
            self._group_index = (self._group_index + max_groups) % len(groups)
            logger.info(f"Rotated groups: {rotated}")
            return rotated

    def can_send_to_group(self, group_id: str, cooldown_seconds: int = 300) -> bool:
        with self._lock:
            last_sent = self._group_last_sent.get(group_id)
            if last_sent is None:
                return True
            import time
            return (time.time() - last_sent) >= cooldown_seconds

    def update_group_sent(self, group_id: str):
        with self._lock:
            self._group_last_sent[group_id] = time.time()
            logger.info(f"Updated last sent for group: {group_id}")

    def increment_daily_count(self):
        with self._lock:
            if time.time() - self._daily_reset_time >= 86400:
                self._daily_count = 0
                self._daily_reset_time = time.time()
                logger.info("Daily group count reset")
            self._daily_count += 1

    def should_stop_daily(self, limit: int = 500) -> bool:
        with self._lock:
            if time.time() - self._daily_reset_time >= 86400:
                self._daily_count = 0
                self._daily_reset_time = time.time()
            return self._daily_count >= limit

    def get_daily_count(self) -> int:
        with self._lock:
            return self._daily_count

    def get_next_message(self, messages: list) -> str:
        with self._lock:
            if not messages:
                return ""
            msg = messages[self._message_index % len(messages)]
            self._message_index += 1
            return msg

    def enable_group_messaging(self) -> bool:
        with self._lock:
            self._group_enabled = True
            logger.info("Group messaging enabled")
            return True

    def disable_group_messaging(self) -> bool:
        with self._lock:
            self._group_enabled = False
            logger.info("Group messaging disabled")
            return True

    def is_group_enabled(self) -> bool:
        with self._lock:
            return self._group_enabled

    def get_group_settings(self) -> dict:
        with self._lock:
            return self._group_settings.copy()

    def update_group_settings(self, settings: dict) -> bool:
        with self._lock:
            self._group_settings.update(settings)
            logger.info(f"Updated group settings: {settings}")
            return True

    def enforce_safe_mode(self):
        with self._lock:
            if self._group_settings.get("safe_mode", True):
                max_groups = self._group_settings.get("max_groups_per_cycle", 5)
                delay_range = self._group_settings.get("delay_range", (30, 90))
                
                if max_groups > 10:
                    self._group_settings["max_groups_per_cycle"] = 10
                    logger.warning("Safe mode: capped max_groups to 10")
                
                if delay_range[0] < 30:
                    self._group_settings["delay_range"] = (30, max(delay_range[1], 30))
                    logger.warning("Safe mode: capped delay min to 30")

    def add_bot(self, bot_name: str, config: dict) -> bool:
        with self._lock:
            if bot_name in self._dynamic_bots:
                logger.warning(f"Bot already exists: {bot_name}")
                return False
            self._dynamic_bots[bot_name] = config
            self._state[bot_name] = {
                "enabled": False,
                "limit": default_limit,
                "count": 0,
                "security_pause": False
            }
            logger.info(f"Added dynamic bot: {bot_name}")
            self.save_bots()
            if MONGO_AVAILABLE and mongo_add_bot:
                mongo_add_bot(bot_name, config)
            return True

    def get_dynamic_bots(self) -> dict:
        with self._lock:
            return self._dynamic_bots.copy()

    def get_all_bots(self) -> dict:
        from config.bots_config import bots as static_bots
        with self._lock:
            return {**static_bots, **self._dynamic_bots}

    def remove_bot(self, bot_name: str) -> bool:
        with self._lock:
            if bot_name in self._dynamic_bots:
                del self._dynamic_bots[bot_name]
                if bot_name in self._state:
                    del self._state[bot_name]
                logger.info(f"Removed dynamic bot: {bot_name}")
                self.save_bots()
                if MONGO_AVAILABLE and mongo_delete_bot:
                    mongo_delete_bot(bot_name)
                return True
            return False

    def update_bot(self, bot_name: str, config: dict) -> bool:
        with self._lock:
            if bot_name in self._dynamic_bots:
                self._dynamic_bots[bot_name].update(config)
                logger.info(f"Updated dynamic bot: {bot_name}")
                self.save_bots()
                return True
            return False

    def bot_exists(self, bot_name: str) -> bool:
        from config.bots_config import bots as static_bots
        return bot_name in static_bots or bot_name in self._dynamic_bots


state_manager = StateManager()