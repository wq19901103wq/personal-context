"""Task and personal calendar pages, each addressed by its own private link."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlparse
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from inputs.devices import DeviceConflict
from briefing import Briefings


TASK_FIELDS = ('id', 'description', 'notes', 'status', 'due_at', 'wake_at', 'timezone',
               'followup_seconds', 'created_at', 'updated_at', 'completed_at')
ASSETS = {'base.css': 'text/css', 'calendar.css': 'text/css', 'calendar.js': 'text/javascript',
          'recommendations.js': 'text/javascript', 'recommendations.css': 'text/css',
          'briefing.js': 'text/javascript', 'briefing.css': 'text/css',
          'devices.js': 'text/javascript', 'swipe.js': 'text/javascript'}


def render_page(filename):
    root = Path(__file__).parent
    page = (root / filename).read_text(encoding='utf-8')
    for name in ('calendar', 'recommendations', 'briefing'):
        page = page.replace(f'<!-- {name} -->', (root / (name + '.html')).read_text(encoding='utf-8'))
    return page


def event_view(event):
    fields = ('id', 'title', 'notes', 'status', 'starts_at', 'ends_at', 'all_day',
              'timezone', 'location', 'time_text', 'updated_at', 'remind_before_minutes',
              'scheduled_date', 'time_period', 'recurrence')
    reminder = event.get('reminder')
    return dict({key: event[key] for key in fields},
        **{key: event[key] for key in ('occurrence_key', 'uncertain') if key in event}, evidence=[
        dict(id=r['id'], stream_id=r['stream_id'], text=r['text'],
             actor=dict(display_name=r['actor'].get('display_name')),
             time=dict(recorded_at=r['time'].get('recorded_at')))
        for r in event.get('evidence', [])], reminder=(
            {key: reminder[key] for key in ('status', 'error', 'sent_at')} if reminder else None))


def calendar_occurrences(app, query):
    args = parse_qs(query)
    tz = ZoneInfo(app.store.settings()['timezone'])
    first = date.fromisoformat(args['start'][0])
    last = date.fromisoformat(args['end'][0])
    if not 0 < (last - first).days <= 62:
        raise ValueError('请选择两个月内的日期范围')
    return [event_view(e) for e in app.store.event_occurrences(
        datetime.combine(first, time(), tz).timestamp(),
        datetime.combine(last, time(), tz).timestamp())]


def calendar_view(app, *, wechat=False):
    tasks = [dict({key: task[key] for key in TASK_FIELDS},
                  detail_path='/task/' + app.store.task_token(task['id']))
             for task in app.store.tasks()]
    insights = [dict({k: item[k] for k in ('id', 'title', 'body', 'reason', 'url', 'kind',
                    'created_at', 'read_at', 'notification_status', 'expires_at', 'discovery', 'feedback', 'not_before')},
                    evidence=[], profile_ids=[]) for item in app.store.insights()]
    return dict(tasks=tasks, events=[event_view(e) for e in app.store.events()], insights=insights,
                briefings=Briefings(app.store).view(),
                wechat=wechat,
                personalization=app.store.personalization(),
                sources=dict(timezone=app.store.settings()['timezone']))


def task_view(app, task):
    result = {key: task[key] for key in TASK_FIELDS}
    result['chat_name'] = task['scope'].get('chat_name')
    origin = task['scope'].get('origin') or {}
    records = {(r['source']['source_id'], r['stream_id'], r['id'], r['content_hash']): r
               for r in [*origin.get('records', []), *app.store.evidence('task', task['id'])]}
    if records:
        result['origin'] = dict(reason=origin.get('reason', ''), records=[
            dict(sender=r['actor'].get('display_name') or '未知发送者', text=r['text'],
                 recorded_at=r['time'].get('recorded_at'),
                 reference=f"会话 {r['stream_id']} · 消息 #{r['id']}")
            for r in records.values()])
    history = []
    for reminder in app.store.inbox(task_id=task['id'], limit=10):
        context = reminder['context']
        messages = []
        for msg in context.get('messages', []):
            ref = msg['ref']
            messages.append(dict(sender=msg['sender'].get('display_name') or '未知发送者',
                                 text=msg.get('content') or msg.get('image_description') or '（无文字内容）',
                                 recorded_at=msg['time'].get('recorded_at'),
                                 reference=f"会话 {ref['chat_id']} · 消息 #{ref['message_id']}"))
        knowledge = context.get('knowledge', {})
        facts = []
        for fact in knowledge.get('facts', []):
            content = fact['fields']
            facts.append(dict(text=content.get('text') or content.get('summary') or
                              content.get('description') or json.dumps(content, ensure_ascii=False),
                              authority=fact['authority'],
                              reference=f"知识 {fact['ref']['document_id']} · {fact['ref']['record_id']}"))
        history.append(dict(id=reminder['id'], body=reminder['body'], created_at=reminder['created_at'],
                            trigger=reminder['trigger'], messages=messages, facts=facts,
                            documents=[dict(title=d['title'], text=d['text'])
                                       for d in knowledge.get('documents', [])],
                            sources_unavailable=bool(context.get('source_errors'))))
    return dict(task=result, history=history, executions=app.store.executions(task['id']))


def task_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, value, status=200, *, html=False, content_type=None):
            content_type = content_type or ('text/html' if html else 'application/json')
            body = (json.dumps(value, ensure_ascii=False) if content_type == 'application/json'
                    else value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                             "form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def request_data(self):
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                self.respond(dict(error='请在助理页面中操作'), 403)
                return None
            if self.headers.get_content_type() != 'application/json':
                self.respond(dict(error='请使用 JSON 请求'), 415)
                return None
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 65536:
                self.respond(dict(error='内容过长或为空'), 413)
                return None
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请提交要修改的字段')
            return data

        def route(self, *, update=False):
            try:
                path = urlparse(self.path).path
                device = re.fullmatch(r'/ingest/devices/([a-f0-9]{32})', path)
                if update and device:
                    authorization = self.headers.get('Authorization', '')
                    token = authorization[7:] if authorization.startswith('Bearer ') else ''
                    app.devices.authorize(device[1], token)
                    data = self.request_data()
                    if data is None:
                        return
                    result = app.devices.receive(device[1], token, data)
                    return self.respond(result, 202 if result['received'] else 200)
                if not update and path.startswith('/assets/'):
                    name = path.removeprefix('/assets/')
                    if name in ASSETS:
                        return self.respond(Path(__file__).with_name(name).read_text(encoding='utf-8'),
                                            content_type=ASSETS[name])
                calendar = re.fullmatch(r'/calendar/([A-Za-z0-9_-]{43})(/data|/occurrences|/personalization|/briefings|/insights/\d+/(?:read|dismiss)|/events(?:/\d+)?|/tasks/\d+)?', path)
                if calendar:
                    app.store.authorize_calendar(calendar[1])
                    suffix = calendar[2]
                    if not update:
                        if suffix is None:
                            return self.respond(render_page('mobile_calendar.html'), html=True)
                        if suffix == '/occurrences':
                            return self.respond(calendar_occurrences(app, urlparse(self.path).query))
                        if suffix == '/data':
                            return self.respond(calendar_view(app, wechat=app.followup.wechat is not None))
                    elif suffix and suffix != '/data':
                        data = self.request_data()
                        if data is None:
                            return
                        if suffix == '/personalization':
                            return self.respond(app.personalize(data))
                        if suffix == '/briefings':
                            return self.respond(Briefings(app.store).configure(data))
                        if suffix.startswith('/insights/'):
                            action = app.store.dismiss_insight if suffix.endswith('/dismiss') else app.store.read_insight
                            action(int(suffix.split('/')[2]))
                            return self.respond(dict(ok=True))
                        if suffix == '/events':
                            return self.respond(event_view(app.create_event(data)), 201)
                        item_id = int(suffix.rsplit('/', 1)[1])
                        if suffix.startswith('/events/'):
                            return self.respond(event_view(app.update_event(item_id, data)))
                        fields = {key: value for key, value in data.items() if key in {
                            'description', 'notes', 'due_at', 'wake_at', 'followup_seconds', 'status', 'deleted'}}
                        task = app.update(item_id, fields)
                        return self.respond({key: task[key] for key in TASK_FIELDS})
                    return self.respond(dict(error='没有这个操作'), 404)
                match = re.fullmatch(r'/task/([A-Za-z0-9_-]{43})(/data)?', path)
                if not match or (update and not match[2]):
                    return self.respond(dict(error='链接不可用'), 404)
                task = app.store.linked_task(match[1])
                if update:
                    # Only same-origin JSON can change state; link previews are read-only.
                    data = self.request_data()
                    if data is None:
                        return
                    task = app.update(task['id'], data)
                if match[2]:
                    return self.respond(task_view(app, task))
                self.respond(Path(__file__).with_name('task.html').read_text(encoding='utf-8'), html=True)
            except PermissionError as exc:
                self.respond(dict(error=str(exc)), 401)
            except DeviceConflict as exc:
                self.respond(dict(error=str(exc)), 409)
            except KeyError:
                self.respond(dict(error='链接或事项不可用'), 404)
            except (ValueError, TypeError) as exc:
                self.respond(dict(error=str(exc)), 400)
            except (OSError, sqlite3.Error):
                self.respond(dict(error='暂时无法读取，请稍后重试'), 503)

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route(update=True)

        def log_message(self, format, *args):
            # URLs contain bearer tokens and must not enter access logs.
            pass

    return Handler


def task_server(app, port):
    return ThreadingHTTPServer(('127.0.0.1', port), task_handler(app))
