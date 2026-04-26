from storage.db import (
    add_bot,
    add_group,
    add_message,
    delete_bot,
    delete_group,
    delete_message,
    get_bots,
    get_group,
    get_message,
    get_setting,
    list_groups,
    list_messages,
    set_group_status,
    set_setting,
)


def get_db():
    return None


def update_group(group_id: str, **kwargs):
    if "status" in kwargs:
        return set_group_status(group_id, kwargs["status"])
    return False


def set_group_special_message(group_id: str, message: str):
    return False


def clear_group_special_message(group_id: str):
    return False


def get_groups():
    return list_groups()


def is_bot_enabled(bot_name: str, default=False):
    return get_setting(f"bot_enabled_{bot_name}", default)


def set_bot_enabled(bot_name: str, enabled: bool):
    return set_setting(f"bot_enabled_{bot_name}", enabled)


def get_bot_count(bot_name: str, default=0):
    return get_setting(f"bot_count_{bot_name}", default)


def set_bot_count(bot_name: str, count: int):
    return set_setting(f"bot_count_{bot_name}", count)


def increment_bot_count(bot_name: str):
    count = int(get_bot_count(bot_name, 0)) + 1
    set_bot_count(bot_name, count)
    return count


def reset_bot_count(bot_name: str):
    return set_bot_count(bot_name, 0)


def get_bot_limit(bot_name: str, default=100):
    return get_setting(f"bot_limit_{bot_name}", default)


def set_bot_limit(bot_name: str, limit: int):
    return set_setting(f"bot_limit_{bot_name}", limit)


def set_bot_security_pause(bot_name: str, paused: bool):
    return set_setting(f"bot_security_pause_{bot_name}", paused)


def is_bot_security_paused(bot_name: str, default=False):
    return get_setting(f"bot_security_pause_{bot_name}", default)
