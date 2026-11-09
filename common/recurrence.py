"""Calendar series stay as data; occurrences are expanded only for a requested range."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def recurrence_rule(value, event):
    if not value:
        return None
    frequency = value['frequency']
    if frequency not in ('daily', 'weekly'):
        raise ValueError('重复可选每天或每周')
    tz = ZoneInfo(event['timezone'])
    anchor = (datetime.fromtimestamp(event['starts_at'], tz).date()
              if event.get('starts_at') is not None else
              date.fromisoformat(event['scheduled_date']) if event.get('scheduled_date') else None)
    if anchor is None:
        raise ValueError('请先为重复日程选择开始日期')
    result = dict(frequency=frequency)
    if frequency == 'weekly':
        days = sorted(set(value.get('weekdays', [anchor.weekday()])))
        count = value.get('weekly_count', len(days))
        if any(type(d) is not int or d not in range(7) for d in days) or not len(days) <= count <= 7 or count < 1:
            raise ValueError('请选择星期，或保留每周天数待定的安排')
        result['weekdays'] = days
        if count > len(days):
            result['weekly_count'] = count
    if value.get('until'):
        until = date.fromisoformat(value['until'])
        if until < anchor:
            raise ValueError('重复结束日期不能早于开始日期')
        result['until'] = until.isoformat()
    return result


def unresolved(event):
    rule = event.get('recurrence') or {}
    return rule.get('weekly_count', 0) > len(rule.get('weekdays', []))


def interval(event):
    """Unknown hours reserve the date; an unknown end reserves the rest of that day."""
    tz = ZoneInfo(event['timezone'])
    start = event.get('starts_at')
    day = (datetime.fromtimestamp(start, tz).date() if start is not None else
           date.fromisoformat(event['scheduled_date']) if event.get('scheduled_date') else None)
    if day is None:
        return None
    return (start if start is not None else datetime.combine(day, time(), tz).timestamp(),
            event.get('ends_at') or datetime.combine(day + timedelta(days=1), time(), tz).timestamp())


def occurrences(events, start, end, *, include_uncertain=False):
    result = []
    for event in events:
        if event.get('deleted_at') is not None or event['status'] == 'cancelled':
            continue
        rule = event.get('recurrence')
        bounds = interval(event)
        if not rule:
            if bounds and bounds[0] < end and bounds[1] > start:
                result.append(dict(event, occurrence_key=f"{event['id']}/{bounds[0]}"))
            continue
        tz = ZoneInfo(event['timezone'])
        anchor = datetime.fromtimestamp(bounds[0], tz)
        finish = datetime.fromtimestamp(bounds[1], tz)
        span = finish.date() - anchor.date()
        first = max(anchor.date(), datetime.fromtimestamp(start, tz).date() - span)
        last = datetime.fromtimestamp(end, tz).date()
        if rule.get('until'):
            last = min(last, date.fromisoformat(rule['until']))
        day = first
        while day <= last:
            known = rule['frequency'] == 'daily' or day.weekday() in rule['weekdays']
            uncertain = not known and unresolved(event)
            if known or (include_uncertain and uncertain):
                a = datetime.combine(day, anchor.timetz()).timestamp()
                b = datetime.combine(day + span, finish.timetz()).timestamp()
                if a < end and b > start:
                    result.append(dict(event, starts_at=a if event['starts_at'] is not None else None,
                        ends_at=b if event['ends_at'] is not None else None, scheduled_date=day.isoformat(),
                        occurrence_key=f"{event['id']}/{day.isoformat()}", uncertain=uncertain))
            day += timedelta(days=1)
    return result
