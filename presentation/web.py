"""One loopback UI with the follow-up loop in the same process."""

from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse
from briefing import Briefings
from .task_web import render_page, task_handler, task_server, calendar_occurrences, event_view, calendar_import_view


def serve(app, *, port=8773, public_port=None, interval=30):
    class Handler(task_handler(app)):
        def parse_request(self):
            if not super().parse_request():
                return False
            allowed_hosts = {f'127.0.0.1:{server.server_port}', f'localhost:{server.server_port}'}
            if self.headers.get('Host') not in allowed_hosts:
                self.respond(dict(error='仅限本机访问'), 403)
                return False
            return True

        def valid_origin(self):
            origin = self.headers.get('Origin')
            allowed = {f'http://127.0.0.1:{server.server_port}', f'http://localhost:{server.server_port}'}
            return origin is None or origin in allowed

        def do_GET(self):
            try:
                url = urlparse(self.path)
                if url.path.startswith(('/task/', '/calendar/', '/assets/')):
                    return self.route()
                if url.path == '/':
                    return self.respond(render_page('index.html'), html=True)
                if url.path == '/api/state':
                    tasks = app.store.tasks()
                    for task in tasks:
                        task['detail_path'] = '/task/' + app.store.task_token(task['id'])
                    calendar_url = (app.followup.public_url + '/calendar/' + app.store.calendar_token()
                                    if app.followup.public_url else None)
                    return self.respond(dict(tasks=tasks, events=[event_view(e) for e in app.store.events()], calendar_url=calendar_url,
                                             calendar_import=calendar_import_view(app.store),
                                             inbox=app.store.inbox(),
                                             sources=app.store.settings(), last_tick=app.followup.last_tick,
                                             last_error=app.followup.last_error, notify=app.followup.notify,
                                             last_delivery=app.followup.last_delivery,
                                             delivery_error=app.followup.delivery_error,
                                             wechat_error=app.followup.wechat_error,
                                             scans=app.store.scans(), extraction_watch=app.store.scan_watch(),
                                             devices=app.devices.list(),
                                             insights=app.store.insights(),
                                             personalization=app.store.personalization(),
                                             briefings=Briefings(app.store).view(wechat=app.followup.wechat is not None),
                                             wechat=app.followup.wechat is not None))
                if url.path == '/api/occurrences':
                    return self.respond(calendar_occurrences(app, url.query))
                if url.path == '/api/chats':
                    query = parse_qs(url.query).get('q', [None])[0]
                    return self.respond(app.chats(query or None))
                if url.path == '/api/questions':
                    return self.respond(app.questions.list())
                if url.path.startswith('/api/events/'):
                    return self.respond(app.store.event(int(url.path.rsplit('/', 1)[1])))
                if url.path.startswith('/api/tasks/'):
                    task_id = int(url.path.rsplit('/', 1)[1])
                    return self.respond(dict(task=app.store.get(task_id),
                                             inbox=app.store.inbox(task_id=task_id),
                                             executions=app.store.executions(task_id)))
                self.respond(dict(error='没有这个页面'), 404)
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                self.respond(dict(error=str(exc)), 400)

        def do_POST(self):
            if urlparse(self.path).path.startswith(('/task/', '/calendar/', '/ingest/devices/')):
                return self.route(update=True)
            try:
                data = self.request_data()
                if data is None:
                    return
                path = urlparse(self.path).path
                if path == '/api/questions':
                    return self.respond(app.questions.ask(data), 202)
                if path.startswith('/api/questions/') and path.endswith('/retry'):
                    return self.respond(app.questions.retry(int(path.split('/')[3])), 202)
                if path == '/api/devices':
                    device = app.devices.create(data)
                    device['url'] = (app.followup.public_url or
                                     f'http://127.0.0.1:{server.server_port}') + device['path']
                    return self.respond(device, 201)
                if path.startswith('/api/devices/'):
                    parts = path.strip('/').split('/')
                    if len(parts) == 4 and parts[3] in ('revoke', 'retry'):
                        action = app.devices.revoke if parts[3] == 'revoke' else app.devices.retry
                        return self.respond(action(parts[2]))
                if path == '/api/tasks':
                    return self.respond(app.create(data), 201)
                if path == '/api/events':
                    return self.respond(app.create_event(data), 201)
                if path.startswith('/api/events/'):
                    return self.respond(app.update_event(int(path.rsplit('/', 1)[1]), data))
                if path == '/api/extract':
                    return self.respond(app.extract(data), 202)
                if path == '/api/extract/stop-watch':
                    app.store.scan_watch(dict(enabled=False))
                    return self.respond(dict(ok=True))
                if path.startswith('/api/profiles/') and path.endswith('/dismiss'):
                    app.store.dismiss_profile(int(path.split('/')[3]))
                    return self.respond(dict(ok=True))
                if path.startswith('/api/insights/') and path.endswith('/read'):
                    app.store.read_insight(int(path.split('/')[3]))
                    return self.respond(dict(ok=True))
                if path.startswith('/api/insights/') and path.endswith('/dismiss'):
                    app.store.dismiss_insight(int(path.split('/')[3]))
                    return self.respond(dict(ok=True))
                if path.startswith('/api/tasks/'):
                    return self.respond(app.update(int(path.rsplit('/', 1)[1]), data))
                if path == '/api/sources':
                    return self.respond(app.set_sources(data))
                if path == '/api/personalization':
                    return self.respond(app.personalize(data))
                if path == '/api/briefings':
                    return self.respond(Briefings(app.store).configure(data))
                if path.startswith('/api/reminders/'):
                    parts = path.strip('/').split('/')
                    if len(parts) == 4 and parts[3] == 'retry-wechat':
                        app.store.retry_wechat(int(parts[2]))
                    else:
                        app.store.read(int(parts[2]))
                    return self.respond(dict(ok=True))
                if path == '/api/tick':
                    return self.respond(dict(reminders=app.followup.tick()))
                self.respond(dict(error='没有这个操作'), 404)
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
                self.respond(dict(error=str(exc)), 400)

        def log_message(self, format, *args):
            # Private links must not enter access logs.
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    public = task_server(app, public_port) if public_port is not None else None
    if public:
        public_worker = threading.Thread(target=public.serve_forever, name='task-page', daemon=True)
        public_worker.start()
    stop = threading.Event()
    worker = threading.Thread(target=app.followup.run, args=(stop,),
                              kwargs=dict(interval=interval), name='follow-up', daemon=True)
    worker.start()
    executor = threading.Thread(target=app.execution.run, args=(stop,), name='task-execution', daemon=True)
    executor.start()
    extractor = threading.Thread(target=app.extraction.run, args=(stop,), name='history-extraction', daemon=True)
    extractor.start()
    recommender = threading.Thread(target=app.proactive.run, args=(stop,), name='proactive', daemon=True)
    recommender.start()
    question_worker = threading.Thread(target=app.questions.run, args=(stop,), name='memory-questions', daemon=True)
    question_worker.start()
    print(f'个人助理：http://127.0.0.1:{server.server_port}/  数据：{app.store.path}', flush=True)
    if public:
        print(f'手机入口监听：127.0.0.1:{public.server_port}；任务详情与私人日程链接', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if public:
            public.shutdown()
            public.server_close()
            public_worker.join(timeout=5)
        server.server_close()
        worker.join(timeout=15)
        executor.join(timeout=20)
        extractor.join(timeout=20)
        recommender.join(timeout=20)
        question_worker.join(timeout=20)
