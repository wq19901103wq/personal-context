"""Shared parsing of structured task and calendar fields."""

from datetime import date, datetime
import re
import time
from zoneinfo import ZoneInfo

from .recurrence import recurrence_rule


def timestamp(value, timezone, *, now=None):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    now = time.time() if now is None else now
    if value == 'now':
        return now
    relative = re.fullmatch(r'\+?(\d+(?:\.\d+)?)(m|h|d)', value)
    if relative:
        return now + float(relative[1]) * {'m': 60, 'h': 3600, 'd': 86400}[relative[2]]
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.timestamp()


def task_fields(data, timezone):
    fields = {}
    for key in ('description', 'notes', 'status'):
        if key in data:
            fields[key] = str(data[key]).strip()
    if 'description' in fields and not fields['description']:
        raise ValueError('请填写待办内容')
    if 'status' in fields and fields['status'] not in ('open', 'waiting', 'done', 'cancelled'):
        raise ValueError('状态可选 open / waiting / done / cancelled')
    for key in ('due_at', 'wake_at'):
        if key in data:
            fields[key] = timestamp(data[key], timezone)
    if 'followup_seconds' in data:
        fields['followup_seconds'] = float(data['followup_seconds'])
        if fields['followup_seconds'] <= 0:
            raise ValueError('跟进间隔应大于零')
    return fields


def event_fields(data, timezone, current=None):
    fields = {}
    for key in ('title', 'notes', 'location', 'time_text', 'status', 'time_period'):
        if key in data:
            fields[key] = str(data[key]).strip()
    if 'title' in fields and not fields['title']:
        raise ValueError('请填写日程标题')
    if 'status' in fields and fields['status'] not in ('tentative', 'confirmed', 'cancelled'):
        raise ValueError('请选择待确认、已确认或已取消')
    for key in ('starts_at', 'ends_at'):
        if key in data:
            fields[key] = timestamp(data[key], timezone)
    if 'scheduled_date' in data:
        fields['scheduled_date'] = date.fromisoformat(data['scheduled_date']).isoformat() if data['scheduled_date'] else None
    if fields.get('starts_at') is not None:
        fields['scheduled_date'] = datetime.fromtimestamp(fields['starts_at'], ZoneInfo(timezone)).date().isoformat()
        fields['time_period'] = ''
    if 'all_day' in data:
        fields['all_day'] = bool(data['all_day'])
    if 'remind_before_minutes' in data:
        value = data['remind_before_minutes']
        fields['remind_before_minutes'] = None if value is None else int(value)
        if value is not None and fields['remind_before_minutes'] < 1:
            raise ValueError('请填写至少提前 1 分钟，或关闭提醒')
    merged = dict(current or {}, **fields)
    start, end = merged.get('starts_at'), merged.get('ends_at')
    if end is not None and (start is None or end <= start):
        raise ValueError('结束时间应晚于开始时间；未确定时可以留空')
    if 'recurrence' in data or merged.get('recurrence'):
        fields['recurrence'] = recurrence_rule(data.get('recurrence', merged.get('recurrence')),
                                               dict(merged, timezone=timezone))
    return fields
