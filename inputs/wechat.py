"""Read-only WeChat history and explicit command reception."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

from common.records import RawRecord
from wechat_reader import open_wechat_history
from wechat_commands import handle

SOURCE_ERRORS = (OSError, ValueError, KeyError, sqlite3.Error)


def history_reader(scope):
    return open_wechat_history(scope['history_db'], account_id=scope['history_account'],
                               source_id=scope.get('history_source'))


def automated_record_ids(scope, records):
    """Match outgoing originals to RPA's delivered replies in the same chat.

    The reader's sender flag identifies an account, not a human author. Optional
    RPA reply state supplies provenance; absent metadata stays unknown. Match
    complete replies near their send time, never wording or first-person style.
    """
    outgoing = [r for r in records if r.actor.is_self is True and r.text]
    if not outgoing:
        return set()
    chats = sorted({r.stream_id for r in outgoing})
    window = 300  # The raw message may be observed shortly after a successful send.
    path = Path(scope['history_db']).expanduser().resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        if not {'replied', 'reply_text', 'reply_time'} <= columns:
            return set()
        rows = db.execute('SELECT c.chatroom_id,m.reply_text,m.reply_time FROM messages m '
            'JOIN chatrooms c ON c.id=m.chatroom_id WHERE c.chatroom_id IN (' +
            ','.join('?' for _ in chats) + ') AND m.replied=1 AND m.reply_text IS NOT NULL '
            'AND m.reply_text NOT IN (\'\',\'(未回复)\') AND m.reply_time BETWEEN ? AND ?',
            (*chats, min(r.time.recorded_at for r in outgoing) - window,
             max(r.time.recorded_at for r in outgoing) + window)).fetchall()
    sends = {}
    for chat, reply, sent_at in rows:
        # RPA stores a delivered batch joined by this separator, as well as single replies.
        for text in {reply, *reply.split(' | ')}:
            sends.setdefault((chat, text), []).append(sent_at)
    return {r.id for r in outgoing if any(abs(r.time.recorded_at - sent_at) <= window
            for sent_at in sends.get((r.stream_id, r.text), ()))}


def history_records(scope, messages):
    messages = tuple(messages)
    records = tuple(RawRecord(
        id=m.ref.message_id, source=m.ref.source, stream_id=m.ref.chat_id,
        kind='wechat_message', actor=m.sender, time=m.time, text=m.content,
        content_hash=m.source_content_hash) for m in messages)
    automated = automated_record_ids(scope, records)
    return tuple(replace(r,
        authorship='automated' if r.id in automated else 'unknown',
        text='\n'.join(part for part in (m.content,
            f'附件内容：{m.image_description}' if m.image_description and m.image_description != m.content
            else None) if part)) for r, m in zip(records, messages))


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
