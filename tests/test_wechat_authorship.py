"""Outgoing account messages need send provenance before they count as automation."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from common.history import HistoryMessage, MessageRef, MessageTime, SenderEvidence, SourceBinding
from inputs.wechat import history_records


class AuthorshipTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'raw.db'
        self.scope = dict(history_db=str(self.path), history_account='self', history_source='sample')
        self.source = SourceBinding('sample', 'self')
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE chatrooms(id INTEGER, chatroom_id TEXT);'
                'INSERT INTO chatrooms VALUES(1,\'chat\'),(2,\'other\');'
                'CREATE TABLE messages(chatroom_id INTEGER,replied INTEGER,reply_text TEXT,reply_time REAL);')

    def message(self, rid, text, *, at=1000, is_self=True, chat='chat'):
        return HistoryMessage(MessageRef(self.source, chat, str(rid)),
            SenderEvidence('self' if is_self else 'peer', '我' if is_self else '朋友', 'source_id', is_self),
            MessageTime(at, None, at), text, 'text', None, False, None, f'hash-{rid}')

    def sent(self, text, *, at=1000, chat=1, replied=1):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?)', (chat, replied, text, at))

    def test_delivered_reply_batches_are_marked_without_guessing_the_text(self):
        self.sent('周末想去看展 | 平时喜欢清淡一点')
        self.sent('下午三点见', replied=0)
        messages = [self.message(1, '周末想去看展'), self.message(2, '平时喜欢清淡一点'),
                    self.message(3, '下午三点见'), self.message(4, '周末想去看展', is_self=False)]
        records = history_records(self.scope, iter(messages))
        self.assertEqual([r.authorship for r in records], ['automated', 'automated', 'unknown', 'unknown'])
        self.assertEqual(records[0].content_hash, 'hash-1')
        self.assertTrue(records[0].actor.is_self)
        self.assertEqual(records[0].text, messages[0].content)

    def test_same_words_in_other_chats_or_at_other_times_are_not_automation(self):
        self.sent('喜欢看展', chat=2)
        self.sent('喜欢摄影', at=1)
        self.sent('我只是建议你喜欢建筑')
        records = history_records(self.scope, [self.message(1, '喜欢看展'),
            self.message(2, '喜欢摄影'), self.message(3, '喜欢建筑')])
        self.assertEqual([r.authorship for r in records], ['unknown'] * 3)

    def test_optional_send_metadata_and_missing_databases_do_not_invent_authorship(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE messages')
            db.execute('CREATE TABLE messages(id INTEGER,content TEXT)')
        self.assertEqual(history_records(self.scope, [self.message(1, '喜欢看展')])[0].authorship, 'unknown')
        missing = self.path.parent / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError):
            history_records(dict(self.scope, history_db=str(missing)), [self.message(1, '喜欢看展')])
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
