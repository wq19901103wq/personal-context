"""JSON commands over the same database used by RPA and DH."""

import argparse
from dataclasses import asdict
import json

from .connection import add_history_options, history_from_args
from .digital_human import recent_draft_input
from common.history import HistoryCursor, HistoryQuery


def main(argv=None):
    parser = argparse.ArgumentParser(description="共用微信历史：会话、最近记录、搜索和 DH 上下文")
    add_history_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    chats = commands.add_parser("chats", help="按最近活跃时间列出会话")
    chats.add_argument("--name", help="会话名称包含的文字")
    chats.add_argument("--limit", type=int, default=100)
    messages = commands.add_parser("messages", help="查询或搜索记录；不传会话则搜索整个来源")
    messages.add_argument("--chat", action="append", default=[])
    messages.add_argument("--contains")
    messages.add_argument("--start", type=float)
    messages.add_argument("--end", type=float)
    messages.add_argument("--newest-first", action="store_true")
    messages.add_argument("--include-revoked", action="store_true")
    messages.add_argument("--cursor", nargs=2, metavar=("RECORDED_AT", "ROW_ID"))
    messages.add_argument("--limit", type=int, default=100)
    recent = commands.add_parser("recent", help="最近 N 条记录，按时间正序返回")
    recent.add_argument("--chat", required=True)
    recent.add_argument("--limit", type=int, default=50)
    draft = commands.add_parser("dh-input", help="导出给现有 DH 草稿入口的 observed 上下文")
    draft.add_argument("--chat", required=True)
    draft.add_argument("--limit", type=int, default=50, help="DH 接受 1 至 200 条上下文")
    draft.add_argument("--request-id", help="指定后输出 draft_stdio.py 的完整请求行")
    args = parser.parse_args(argv)
    history = history_from_args(args)
    if args.command == "chats":
        result = [asdict(chat) for chat in history.list_chats(name=args.name, limit=args.limit)]
    elif args.command == "recent":
        result = [asdict(message) for message in history.recent(args.chat, limit=args.limit)]
    elif args.command == "dh-input":
        result = recent_draft_input(history, args.chat, limit=args.limit)
        if args.request_id is not None:
            result = {"request_id": args.request_id, "input": result}
    else:
        cursor = (HistoryCursor(float(args.cursor[0]), int(args.cursor[1]))
                  if args.cursor else None)
        result = asdict(history.query(HistoryQuery(
            chat_ids=tuple(args.chat), start_recorded_at=args.start, end_recorded_at=args.end,
            include_revoked=args.include_revoked, limit=args.limit,
            contains=args.contains, newest_first=args.newest_first,
        ), cursor=cursor))
    print(json.dumps(result, ensure_ascii=False))
    return 0
