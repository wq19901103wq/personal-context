"""Daily schedules and coalesced changes, derived only from saved structured items."""

from datetime import datetime, time as day_time, timedelta
import json
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from memory.personalization import next_recommendation_time
from common.recurrence import occurrences, interval, unresolved
from personal_context.wechat_push import SESSION_ERRORS


EVENT_FIELDS = ('id', 'title', 'status', 'starts_at', 'ends_at', 'all_day', 'timezone',
                'location', 'scheduled_date', 'time_period', 'recurrence', 'deleted_at')
TASK_FIELDS = ('id', 'description', 'status', 'due_at', 'timezone', 'deleted_at')
PERIODS = dict(morning='上午', noon='中午', afternoon='下午', evening='晚上', night='深夜')
STATUSES = dict(open='进行中', waiting='等待回复', done='已完成', cancelled='已取消',
                tentative='待确认', confirmed='已确认')


def bounds(now, timezone, days=1):
    tz = ZoneInfo(timezone)
    day = datetime.fromtimestamp(now, tz).date()
    return (day.isoformat(), datetime.combine(day, day_time(), tz).timestamp(),
            datetime.combine(day + timedelta(days=days), day_time(), tz).timestamp())


def stamp(value, timezone):
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime('%m-%d %H:%M')


def event_text(event):
    tz = event['timezone']
    if event['all_day']:
        when = (datetime.fromtimestamp(event['starts_at'], ZoneInfo(tz)).date().isoformat()
                if event['starts_at'] is not None else event['scheduled_date'] or '日期待确认') + ' 全天'
    elif event['starts_at'] is not None:
        when = stamp(event['starts_at'], tz)
        when += '–' + stamp(event['ends_at'], tz) if event['ends_at'] else '（结束时间待确认）'
    else:
        when = (event['scheduled_date'] or '日期待确认') + ' ' + PERIODS.get(event['time_period'], '')
        when += '（具体时间待确认）'
    if event.get('recurrence'):
        rule = event['recurrence']
        when += ' · ' + ('每天' if rule['frequency'] == 'daily' else '每周' +
                         '、'.join('一二三四五六日'[d] for d in rule['weekdays']))
        if unresolved(event):
            when += f"（每周 {rule['weekly_count']} 天，部分日期待确认）"
        if rule.get('until'):
            when += '，至 ' + rule['until']
    if event.get('uncertain'):
        when += ' · 今天是否安排待确认'
    when += ' · ' + tz
    return (event['title'] + ' · ' + when + ' · ' +
            (event['location'] or '地点待确认') +
            (' · 待确认' if event['status'] == 'tentative' else ''))


def task_text(task, now):
    due = task['due_at']
    timing = ('截止 ' + stamp(due, task['timezone']) + ' · ' + task['timezone'] +
              ('（已逾期）' if due < now else '')) if due is not None else '未设截止时间'
    return task['description'] + ' · ' + timing + ' · ' + STATUSES[task['status']]


def relevant(item, now, timezone):
    if not item or item.get('deleted_at') is not None:
        return False
    _, start, end = bounds(now, timezone, 7)
    if 'description' in item:
        return item['status'] in ('open', 'waiting') and (item['due_at'] is None or item['due_at'] < end)
    return bool(occurrences([item], start, end, include_uncertain=True))


def delta(before, after, now, timezone):
    changes = {}
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        if a != b and (relevant(a, now, timezone) or relevant(b, now, timezone)):
            changes[key] = dict(before=a, after=b)
    return changes


def change_items(changes, now):
    result = []
    for key, value in sorted(changes.items()):
        a, b = value['before'], value['after']
        item = b or a
        kind, item_id = key.split(':')
        describe = event_text if kind == 'event' else lambda v: task_text(v, now)
        removed = not b or b.get('deleted_at') is not None
        added = not a or a.get('deleted_at') is not None
        label = '已删除' if removed else '新增' if added else '已取消' if b['status'] == 'cancelled' else '已更新'
        old = describe(a) if not added else None
        new = describe(b) if not removed else None
        text = label + ' · ' + (new or old)
        if old and new:
            text = label + '\n原：' + old + '\n现：' + new
        result.append(dict(kind=kind, id=int(item_id), text=text))
    return result


class Briefings:
    def __init__(self, store, *, wechat=None):
        self.store = store
        self.wechat = wechat
        self._daily_cache = None

    @staticmethod
    def read_setting(db, key, default):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def write_setting(db, key, value):
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value, ensure_ascii=False)))

    def config_in(self, db):
        return {'daily_enabled': True, 'changes_enabled': True,
                **self.read_setting(db, 'briefing_config', {})}

    def configure(self, value):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config_in(db)
            previous = dict(config)
            for key in ('daily_enabled', 'changes_enabled'):
                if key in value:
                    if type(value[key]) is not bool:
                        raise ValueError('请使用开启或关闭选项')
                    config[key] = value[key]
            self.write_setting(db, 'briefing_config', config)
            if previous['changes_enabled'] != config['changes_enabled']:
                self.write_setting(db, 'briefing_monitor', dict(snapshot=self.snapshot_in(db), pending={}, changed_at=time.time()))
                db.execute("UPDATE briefing_deliveries SET status='disabled' WHERE kind='changes' AND status IN ('pending','retry')")
            if not config['daily_enabled']:
                db.execute("UPDATE briefing_deliveries SET status='disabled' WHERE kind='daily' AND status IN ('pending','retry')")
            return config

    def snapshot_in(self, db):
        result = {}
        for row in db.execute('SELECT ' + ','.join(TASK_FIELDS) + ' FROM tasks'):
            result['task:' + str(row['id'])] = dict(row)
        columns = ','.join('recurrence_json' if k == 'recurrence' else k for k in EVENT_FIELDS)
        for row in db.execute('SELECT ' + columns + ' FROM calendar_events'):
            event = dict(row)
            event['all_day'] = bool(event['all_day'])
            event['recurrence'] = json.loads(event.pop('recurrence_json') or 'null')
            result['event:' + str(row['id'])] = event
        return result

    def daily(self, snapshot, now, timezone):
        day, start, end = bounds(now, timezone)
        events = occurrences([v for k, v in snapshot.items() if k.startswith('event:')],
                             start, end, include_uncertain=True)
        events.sort(key=lambda e: interval(e)[0])
        tasks = [v for k, v in snapshot.items() if k.startswith('task:') and
                 v['deleted_at'] is None and v['status'] in ('open', 'waiting') and
                 (v['due_at'] is None or v['due_at'] < end or v['status'] == 'waiting')]
        tasks.sort(key=lambda t: (t['due_at'] is None, t['due_at'] or 0, t['id']))
        unknown = [v for k, v in snapshot.items() if k.startswith('event:') and
                   v['deleted_at'] is None and v['status'] != 'cancelled' and interval(v) is None]
        sections = [dict(title='今天的日程', items=[dict(kind='event', id=e['id'], text=event_text(e)) for e in events]),
                    dict(title='今天要关注的待办', items=[dict(kind='task', id=t['id'], text=task_text(t, now)) for t in tasks])]
        if unknown:
            sections.append(dict(title='日期待确认（未计入今天）', items=[
                dict(kind='event', id=e['id'], text=event_text(e)) for e in unknown]))
        title = day + ' 每日简报'
        body = '\n\n'.join(s['title'] + '\n' + ('\n'.join('• ' + i['text'] for i in s['items']) or '暂无记录')
                           for s in sections)
        return dict(day=day, title=title, body=body, sections=sections, updated_at=now)

    def sync(self, now):
        timezone = self.store.settings()['timezone']
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            config, snapshot = self.config_in(db), self.snapshot_in(db)
            monitor = self.read_setting(db, 'briefing_monitor', None)
            pending = dict(monitor['pending']) if monitor else {}
            observed = delta(monitor['snapshot'], snapshot, now, timezone) if monitor else {}
            for key in pending:
                if monitor['snapshot'].get(key) != snapshot.get(key):
                    observed[key] = dict(before=monitor['snapshot'].get(key), after=snapshot.get(key))
            changed_at = monitor['changed_at'] if monitor else now
            for key, change in observed.items():
                original = pending.get(key, change)['before']
                pending[key] = dict(before=original, after=change['after'])
                changed_at = now
            pending = {k: v for k, v in pending.items() if v['before'] != v['after'] and
                       (relevant(v['before'], now, timezone) or relevant(v['after'], now, timezone))}
            if not config['changes_enabled']:
                pending = {}
                db.execute("UPDATE briefing_deliveries SET status='disabled' WHERE kind='changes' AND status IN ('pending','retry')")
            updated = dict(snapshot=snapshot, pending=pending, changed_at=changed_at)
            if updated != monitor:
                self.write_setting(db, 'briefing_monitor', updated)
            day = bounds(now, timezone)[0]
            db.execute("UPDATE briefing_deliveries SET status='expired' WHERE kind='daily' AND day!=? AND status IN ('pending','retry')", (day,))
            if config['daily_enabled']:
                row = db.execute("SELECT id,status,body,items_json FROM briefing_deliveries WHERE kind='daily' AND day=?", (day,)).fetchone()
                if row is None or row['status'] in ('pending', 'retry', 'disabled'):
                    overdue = frozenset(k for k, v in snapshot.items() if k.startswith('task:') and
                                        v['deleted_at'] is None and v['status'] in ('open', 'waiting') and
                                        v['due_at'] is not None and v['due_at'] < now)
                    key = (snapshot, day, timezone, overdue)
                    if self._daily_cache is None or self._daily_cache[0] != key:
                        daily = self.daily(snapshot, now, timezone)
                        self._daily_cache = (key, daily, json.dumps(daily['sections'], ensure_ascii=False))
                    _, daily, items_json = self._daily_cache
                    if row is None:
                        db.execute("INSERT INTO briefing_deliveries (kind,day,title,body,items_json,client_id,created_at) VALUES ('daily',?,?,?,?,?,?)",
                                   (day, daily['title'], daily['body'], items_json, 'briefing-' + uuid4().hex, now))
                    elif row['status'] == 'disabled' or (row['body'], row['items_json']) != (daily['body'], items_json):
                        db.execute("UPDATE briefing_deliveries SET body=?,items_json=?,status=CASE WHEN status='disabled' THEN 'pending' ELSE status END WHERE id=?",
                                   (daily['body'], items_json, row['id']))
            else:
                db.execute("UPDATE briefing_deliveries SET status='disabled' WHERE kind='daily' AND status IN ('pending','retry')")

    def waiting_reason(self, db, monitor, now, timezone):
        """Use the same scheduling gate for delivery and its short page explanation."""
        _, _, end = bounds(now, timezone)
        events = [v for k, v in monitor['snapshot'].items() if k.startswith('event:')]
        due = next_recommendation_time(events, now, end, timezone)
        if due != now:
            daytime = next_recommendation_time([], now, end, timezone)
            if daytime is None:
                return '已过今日推送时段（10:00–21:00），简报保留在这里'
            if due is None:
                return '今天没有避开日程及前后 30 分钟的推送空档，简报保留在这里'
            if daytime > now:
                return '未到推送时段（10:00–21:00），等待合适空档'
            return '当前日程及前后 30 分钟内不推送，等待空档'
        last = db.execute('SELECT MAX(sent_at) FROM briefing_deliveries').fetchone()[0]
        if last is not None and now - last < 1800:
            return '距上次简报或变化推送不足 30 分钟，稍后再发'
        return None

    def claim(self, now):
        timezone = self.store.settings()['timezone']
        day = bounds(now, timezone)[0]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config_in(db)
            monitor = self.read_setting(db, 'briefing_monitor', None)
            if not monitor:
                return None
            if self.waiting_reason(db, monitor, now, timezone):
                return None
            row = db.execute("SELECT * FROM briefing_deliveries WHERE status IN ('pending','retry') AND retry_at<=? AND (kind='changes' OR day=?) ORDER BY id LIMIT 1", (now, day)).fetchone()
            if row and not config[row['kind'] + '_enabled']:
                return None
            if row and row['kind'] == 'changes':
                # Retry a known rejection using the latest state, coalescing intervening edits.
                changes = json.loads(row['items_json'])
                before = dict(monitor['snapshot'])
                for key, change in dict(monitor['pending'], **changes).items():
                    if change['before'] is None:
                        before.pop(key, None)
                    else:
                        before[key] = change['before']
                monitor['pending'] = delta(before, monitor['snapshot'], now, timezone)
            if row is None or row['kind'] == 'changes':
                if not config['changes_enabled'] or now - monitor['changed_at'] < 120:
                    return None
                changes = monitor['pending']
                if not changes:
                    if row:
                        db.execute("UPDATE briefing_deliveries SET status='superseded' WHERE id=?", (row['id'],))
                        self.write_setting(db, 'briefing_monitor', monitor)
                    return None
                body = '\n\n'.join(i['text'] for i in change_items(changes, now))
                values = ('近期安排有变化', body, json.dumps(changes, ensure_ascii=False))
                if row:
                    db.execute('UPDATE briefing_deliveries SET title=?,body=?,items_json=? WHERE id=?', (*values, row['id']))
                    item_id = row['id']
                else:
                    item_id = db.execute("INSERT INTO briefing_deliveries (kind,day,title,body,items_json,client_id,created_at) VALUES ('changes',?,?,?,?,?,?)",
                        (day, *values, 'briefing-' + uuid4().hex, now)).lastrowid
                monitor['pending'] = {}
                self.write_setting(db, 'briefing_monitor', monitor)
            else:
                item_id = row['id']
            db.execute("UPDATE briefing_deliveries SET status='sending',sent_at=?,attempts=attempts+1,error=NULL,recipient_id=COALESCE(recipient_id,?),bot_id=COALESCE(bot_id,?) WHERE id=?",
                       (now, self.wechat.recipient_id if self.wechat else None,
                        self.wechat.bot_id if self.wechat else None, item_id))
            return dict(db.execute('SELECT * FROM briefing_deliveries WHERE id=?', (item_id,)).fetchone())

    def result(self, item_id, status, now, error=None):
        with self.store.connect() as db:
            db.execute("UPDATE briefing_deliveries SET status=CASE WHEN ?='retry' AND attempts>=3 THEN 'failed' ELSE ? END,error=?,retry_at=?,sent_at=CASE WHEN ?='retry' THEN NULL ELSE sent_at END WHERE id=? AND status='sending'",
                       (status, status, error, now + 60, status, item_id))

    def view(self, now=None, *, wechat=True):
        now = time.time() if now is None else now
        timezone = self.store.settings()['timezone']
        with self.store.connect() as db:
            config, snapshot = self.config_in(db), self.snapshot_in(db)
            monitor = self.read_setting(db, 'briefing_monitor', {})
            history = [dict(row) for row in db.execute('SELECT id,kind,day,title,body,status,error,created_at,sent_at FROM briefing_deliveries ORDER BY id DESC LIMIT 20')]
            day = bounds(now, timezone)[0]
            waiting = self.waiting_reason(db, monitor or dict(snapshot=snapshot), now, timezone)

        def note(kind):
            if not config[kind + '_enabled']:
                return '推送已暂停，内容保留在这里'
            item = next((v for v in history if v['kind'] == kind and
                         (kind != 'daily' or v['day'] == day)), None)
            if kind == 'changes' and monitor.get('pending') and (
                    not item or item['status'] not in ('pending', 'retry', 'sending')):
                item = None
            elif kind == 'changes' and not item:
                return None
            status = item['status'] if item else 'pending'
            if status in ('delivered', 'sending', 'expired', 'superseded'):
                return None
            if not wechat:
                return '当前未连接微信，内容保留在这里'
            error = item.get('error') if item else None
            if status in ('retry', 'failed') and error in SESSION_ERRORS:
                return error.replace('后重试', '后会自动恢复')
            if status in ('retry', 'failed', 'unknown'):
                return error or {'retry': '推送未完成，等待重试',
                                 'failed': '推送失败，内容保留在这里',
                                 'unknown': '发送结果未知，请查看微信；不会自动重发'}[status]
            if waiting:
                return waiting
            if kind == 'changes' and now - monitor.get('changed_at', now) < 120:
                return '正在合并变化，连续 2 分钟稳定后推送'
            return '等待下一轮推送'

        return dict(config=config, today=self.daily(snapshot, now, timezone),
                    pending=change_items(monitor.get('pending', {}), now), history=history,
                    delivery_notes=dict(daily=note('daily'), changes=note('changes')))
