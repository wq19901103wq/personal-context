"""Local tasks, follow-up inbox and optional ClawBot WeChat notifications."""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading

from app import Assistant
from integrations import Integrations
from presentation.web import serve
from personal_context.wechat_push import PushError


DEFAULT_DB = Path(__file__).resolve().parent / 'data/assistant.db'


def main(argv=None):
    parser = argparse.ArgumentParser(description='个人助理：待办与主动跟进')
    parser.add_argument('--db', type=Path, default=os.environ.get('PERSONAL_ASSISTANT_DB', DEFAULT_DB))
    parser.add_argument('--notify', action='store_true', help='把新提醒同时交给 macOS 通知中心')
    parser.add_argument('--wechat', action='store_true', help='通过已有 ClawBot 登录向本人微信推送新提醒')
    parser.add_argument('--wechat-credentials', type=Path, help='ClawBot 登录目录，默认 ~/.mcp-wechat-server')
    parser.add_argument('--public-url', default=os.environ.get('PERSONAL_ASSISTANT_PUBLIC_URL'),
                        help='手机详情入口的 HTTPS 根地址；需要将隧道指向独立 public-port')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add', help='创建待办')
    add.add_argument('description')
    add.add_argument('--notes', default='')
    add.add_argument('--execute', action='store_true', help='创建后让后台助理分步推进')
    add.add_argument('--due', dest='due_at', help='ISO 本地时间或 30m / 2h / 1d')
    add.add_argument('--wake', dest='wake_at', help='下次跟进；默认截止时间，无截止则立即')
    add.add_argument('--every-hours', type=float, default=24, help='未完成时的跟进间隔')
    add.add_argument('--chat', dest='chat_id', help='关联微信会话；新收到消息也会提醒')
    add.add_argument('--knowledge-query', help='读取相关人物、事实和 Wiki 的关键词')
    add.add_argument('--person-account', help='知识中明确的对方账号；不会按昵称归并')
    commands.add_parser('list', help='列出待办')
    show = commands.add_parser('show', help='查看待办与跟进来源')
    show.add_argument('id', type=int)
    advance = commands.add_parser('advance', help='提交复杂需求，后台分步推进或继续之前的任务')
    advance.add_argument('id', type=int)
    advance.add_argument('instruction')
    halt = commands.add_parser('stop', help='停止当前执行，保留已有进展')
    halt.add_argument('id', type=int)
    for name in ('done', 'cancel', 'reopen', 'wait'):
        command = commands.add_parser(name)
        command.add_argument('id', type=int)
        if name == 'wait':
            command.add_argument('--until', default='1d', help='下一次跟进时间')
    snooze = commands.add_parser('snooze', help='调整下次定时跟进；微信新消息仍会提醒')
    snooze.add_argument('id', type=int)
    snooze.add_argument('until', help='例如 1h 或 2026-11-06T10:00')
    inbox = commands.add_parser('inbox')
    inbox.add_argument('--unread', action='store_true')
    read = commands.add_parser('read')
    read.add_argument('id', type=int)
    retry = commands.add_parser('retry-wechat', help='重试未读提醒的微信投递失败，结果未知不重发')
    retry.add_argument('id', type=int, help='提醒 ID')
    sources = commands.add_parser('sources', help='查看或保存默认来源，新待办绑定当时的配置')
    sources.add_argument('--history-db')
    sources.add_argument('--history-account')
    sources.add_argument('--history-source')
    sources.add_argument('--dh-knowledge', action='append')
    sources.add_argument('--rpa-memory', action='append')
    sources.add_argument('--timezone')
    chats = commands.add_parser('chats')
    chats.add_argument('--contains')
    extract = commands.add_parser('extract', help='从已有微信记录提取待办、人物画像和相关建议')
    extract.add_argument('--chat', dest='chat_ids', action='append', help='可重复；默认最近 5 个会话')
    extract.add_argument('--hours', type=float, default=168, help='最近多少小时，默认 7 天')
    extract.add_argument('--watch', action='store_true', default=None, help='每小时自动提取这些会话的新记录')
    extract.add_argument('--watch-all', action='store_true', default=None, help='从现在起，每分钟发现所有会话的新增活动')
    commands.add_parser('extractions', help='查看最近提取结果')
    review = commands.add_parser('review-extractions', help='用原始上下文重新判断已有自动提取，撤下误项')
    review.add_argument('--chat', dest='chat_ids', action='append', help='可重复；默认已有自动提取的会话')
    commands.add_parser('stop-extraction-watch', help='停止自动提取')
    commands.add_parser('tick', help='立即检查一次')
    recommend = commands.add_parser('recommend', help='立即检索并保存推荐；不占后台日额度，仍按去重与推送时段处理')
    recommend.add_argument('--preview', action='store_true', help='只检索预览，不保存推荐或发微信')
    commands.add_parser('integrations', help='查看外部接口与 MCP 配置状态，不输出凭据、不联网')
    calendar = commands.add_parser('calendar', help='只读导入本机 Apple／iCloud 日历')
    calendar.add_argument('action', choices=('status', 'authorize', 'enable', 'disable', 'sync'), default='status', nargs='?')
    calendar.add_argument('--calendar', dest='calendar_ids', action='append', help='enable 时指定日历 ID，可重复；默认全部本机账户日历')
    worker = commands.add_parser('worker', help='持续跟进，无页面')
    worker.add_argument('--interval', type=float, default=30)
    web = commands.add_parser('serve', help='本地页面与跟进进程')
    web.add_argument('--port', type=int, default=8773)
    web.add_argument('--public-port', type=int, help='仅提供单任务详情的本机端口，供 HTTPS 隧道连接')
    web.add_argument('--interval', type=float, default=30)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.command == 'integrations':
            print(json.dumps(Integrations(args.db.parent).status(), ensure_ascii=False, indent=2))
            return 0
        app = Assistant(args.db, notify=args.notify, wechat=args.wechat,
                        wechat_credentials=args.wechat_credentials, public_url=args.public_url)
        if args.command == 'calendar':
            calendar = app.followup.calendar
            if args.action == 'authorize':
                result = calendar.reader.authorize()
            elif args.action in ('enable', 'disable'):
                result = calendar.configure(enabled=args.action == 'enable',
                    calendar_ids=(args.calendar_ids or []) if args.action == 'enable' else None)
            elif args.action == 'sync':
                with app.store.worker_lock():
                    result = calendar.sync(force=True)
                if result.get('error'):
                    raise ValueError(result['error'])
            else:
                result = calendar.memory.status()
                if calendar.reader.executable.is_file():
                    result.update(calendar.reader.run('status'))
        elif args.command == 'add':
            fields = vars(args).copy()
            fields['followup_seconds'] = args.every_hours * 3600
            result = app.create(fields)
        elif args.command == 'list':
            result = app.store.tasks()
        elif args.command == 'show':
            result = dict(task=app.store.get(args.id), inbox=app.store.inbox(task_id=args.id),
                          executions=app.store.executions(args.id))
        elif args.command in ('advance', 'stop'):
            result = app.update(args.id, dict(execution_action='start' if args.command == 'advance' else 'stop',
                                             instruction=getattr(args, 'instruction', '')))
        elif args.command in ('done', 'cancel', 'reopen', 'wait', 'snooze'):
            fields = {}
            if args.command != 'snooze':
                fields['status'] = dict(done='done', cancel='cancelled', reopen='open', wait='waiting')[args.command]
            if args.command in ('wait', 'snooze'):
                fields['wake_at'] = args.until
            result = app.update(args.id, fields)
        elif args.command == 'inbox':
            result = app.store.inbox(unread_only=args.unread)
        elif args.command == 'read':
            app.store.read(args.id)
            result = dict(ok=True)
        elif args.command == 'retry-wechat':
            app.store.retry_wechat(args.id)
            result = dict(ok=True)
        elif args.command == 'sources':
            values = {key: value for key, value in vars(args).items() if value is not None and key in {
                'history_db', 'history_account', 'history_source', 'dh_knowledge', 'rpa_memory', 'timezone'}}
            result = app.set_sources(values) if values else app.store.settings()
        elif args.command == 'chats':
            result = app.chats(args.contains)
        elif args.command == 'extract':
            result = app.extract({k: v for k, v in vars(args).items()
                                  if k in ('chat_ids', 'hours', 'watch', 'watch_all') and v is not None})
        elif args.command == 'extractions':
            result = dict(scans=app.store.scans(), watch=app.store.scan_watch())
        elif args.command == 'review-extractions':
            result = dict(scan_ids=app.extraction.queue_review(args.chat_ids))
        elif args.command == 'stop-extraction-watch':
            result = app.store.scan_watch(dict(enabled=False))
        elif args.command == 'tick':
            result = dict(reminders=app.followup.tick())
        elif args.command == 'recommend':
            stop = threading.Event()
            if args.preview:
                result = app.proactive.preview(stop)
            else:
                run = app.proactive.tick(stop, requested=True)
                result = dict(run=run, insights=app.store.insights())
                if run['status'] == 'failed':
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 1
        elif args.command in ('serve', 'worker'):
            if args.interval <= 0:
                raise ValueError('轮询间隔应大于零')
            # launchd stops jobs with SIGTERM; use the same cleanup as Ctrl-C.
            signal.signal(signal.SIGTERM, signal.default_int_handler)
            if args.command == 'serve':
                serve(app, port=args.port, public_port=args.public_port, interval=args.interval)
            else:
                stop = threading.Event()
                executor = threading.Thread(target=app.execution.run, args=(stop,), daemon=True)
                executor.start()
                extractor = threading.Thread(target=app.extraction.run, args=(stop,), daemon=True)
                extractor.start()
                recommender = threading.Thread(target=app.proactive.run, args=(stop,), daemon=True)
                recommender.start()
                try:
                    app.followup.run(stop, interval=args.interval)
                except KeyboardInterrupt:
                    pass
                finally:
                    stop.set()
                    executor.join(timeout=20)
                    extractor.join(timeout=20)
                    recommender.join(timeout=20)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error, PushError) as exc:
        parser.exit(2, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
