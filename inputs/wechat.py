"""Read-only WeChat history and explicit command reception."""

import sqlite3

from wechat_reader import open_wechat_history
from wechat_commands import handle

SOURCE_ERRORS = (OSError, ValueError, KeyError, sqlite3.Error)


def history_reader(scope):
    return open_wechat_history(scope['history_db'], account_id=scope['history_account'],
                               source_id=scope.get('history_source'))


def recent_messages(scope):
    return history_reader(scope).recent(scope['chat_id'], limit=30)


def message_ids(messages):
    return [message.ref.message_id for message in messages]


def receive_commands(store, wechat, now, *, public_url=None):
    state = store.wechat_state(wechat, now=now)
    result = wechat.poll(state['cursor'])
    state = store.consume_wechat(wechat, state, result,
        lambda db, msg, stamp: handle(store, wechat, db, msg, stamp, public_url=public_url), now=now)
    wechat.saved_context = wechat.state().get('contextTokens', {}).get(
        f'{wechat.bot_id}:{wechat.recipient_id}')
    wechat.context = state['context'] or wechat.saved_context
