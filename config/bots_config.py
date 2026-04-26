from storage.db import get_bots, list_messages

default_limit = 100


def get_all_bots():
    return get_bots()


def messages():
    return [message["content"] for message in list_messages()]
