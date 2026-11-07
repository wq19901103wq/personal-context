"""Explicit WeChat controls; ordinary conversation is left to ClawBot."""

from datetime import datetime
import re
from zoneinfo import ZoneInfo


HELP = ('个人助理可以接收：\n/待办\n/日程\n/完成 12\n/取消 12\n'
        '/改期 12 2h\n时间也可写 2026-11-06T10:00（待办所在时区）。\n'
        '引用一条助理提醒时可以省略编号。复杂编辑请打开提醒中的详情链接。')


def quoted_task(db, wechat, msg):
    refs = [msg.get('ref_msg')] + [item.get('ref_msg') for item in msg.get('item_list', [])]
    for ref in refs:
        if not ref:
            continue
        text = ref.get('message_item', {}).get('text_item', {}).get('text') or ref.get('title', '')
        match = re.match(r'^个人助理 · 待办 #(\d+)\s*\n([^\n]+)', text)
        if not match:
            continue
        row = db.execute("SELECT 1 FROM reminders r JOIN wechat_deliveries w ON w.reminder_id=r.id "
                         "WHERE r.task_id=? AND r.title=? AND w.bot_id=? AND w.recipient_id=? "
                         "AND w.status IN ('delivered','unknown')",
                         (int(match[1]), match[2], wechat.bot_id, wechat.recipient_id)).fetchone()
        if row:
            return int(match[1])
    return None


def handle(store, wechat, db, msg, now, *, public_url=None):
    text = '\n'.join(item.get('text_item', {}).get('text', '')
                     for item in msg.get('item_list', []) if item.get('type') == 1).strip()
    parts = text.split(maxsplit=1)
    if not parts or parts[0] not in ('/待办', '/日程', '/帮助', '/完成', '/取消', '/改期'):
        return None
    command, args = parts[0], parts[1].strip() if len(parts) > 1 else ''
    if command == '/帮助':
        return None, command, HELP
    if command == '/日程':
        if not public_url:
            return None, command, '手机入口尚未配置，请先在本机助理中查看日程。'
        path = '/calendar/' + store.calendar_token_in(db)
        return None, command, '个人日程表：\n' + public_url + path + '\n\n可查看、改期或取消日程，也可调整待办。请只在自己的设备上打开。'
    if command == '/待办':
        rows = db.execute("SELECT id,description FROM tasks WHERE status IN ('open','waiting') "
                          'ORDER BY COALESCE(wake_at,1e30),id LIMIT 21').fetchall()
        reply = '当前没有进行中的待办。' if not rows else '进行中的待办：\n' + '\n'.join(
            f"#{row['id']} {row['description']}" for row in rows[:20])
        if len(rows) > 20:
            reply += '\n其余待办请在本地助理中查看。'
        return None, command, reply + '\n\n用 /帮助 查看操作方式。'
    quoted = quoted_task(db, wechat, msg)
    explicit = re.match(r'^#?(\d+)(?:\s+|$)', args)
    task_id = int(explicit[1]) if explicit else quoted
    value = args[explicit.end():].strip() if explicit else args
    if explicit and quoted is not None and task_id != quoted:
        return None, command, '编号与引用的提醒不同，没有修改。请使用同一个待办编号。'
    if task_id is None:
        return None, command, '请写待办编号，或引用助理提醒。\n例如 /完成 12 或 /改期 12 2h'
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return None, command, f'待办 #{task_id} 不存在，没有修改。'
    if row['status'] in ('done', 'cancelled'):
        return task_id, command, f"待办 #{task_id} 已{'完成' if row['status'] == 'done' else '取消'}。"
    if command == '/改期':
        from common.fields import timestamp
        try:
            until = timestamp(value, row['timezone'], now=now)
            if until is None or until <= now:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return task_id, command, '时间没有识别，没有修改。请写 /改期 12 2h 或 /改期 12 2026-11-06T10:00'
        store.update_in(db, task_id, dict(due_at=until, wake_at=until), now=now)
        stamp = datetime.fromtimestamp(until, ZoneInfo(row['timezone'])).strftime('%m-%d %H:%M %Z')
        return task_id, command, f'已将待办 #{task_id} 的期限与下次跟进改为 {stamp}。'
    if value:
        return task_id, command, '没有修改。请使用 /完成 编号 或 /取消 编号。'
    status = 'done' if command == '/完成' else 'cancelled'
    store.update_in(db, task_id, dict(status=status), now=now)
    return task_id, command, f"待办 #{task_id} 已{'完成' if status == 'done' else '取消'}，停止跟进。"
