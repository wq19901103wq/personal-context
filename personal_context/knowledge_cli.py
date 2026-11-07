"""Shared knowledge commands for personal assistants, RPA and DH."""

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path

from .dh_knowledge import open_dh_knowledge
from .knowledge import combine_knowledge
from .rpa_knowledge import open_rpa_knowledge
from .assistant_knowledge import open_assistant_knowledge


def main(argv=None):
    parser = argparse.ArgumentParser(description="共用当前知识快照：人物、事实和 Wiki 文档")
    parser.add_argument("--dh-knowledge", type=Path, action="append", default=[],
                        help="显式选择 DH Share 目录或原生知识 JSON；可重复")
    parser.add_argument("--rpa-memory", type=Path, action="append", default=[],
                        help="RPA data/memory 目录；可重复")
    parser.add_argument("--account", default="unspecified", help="所选 RPA 目录的账号标识")
    parser.add_argument("--assistant-db", type=Path, action="append", default=[],
                        help="个人助理数据库中的派生人物画像；可重复")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("summary", help="仅输出来源和数量")
    for name in ("people", "facts", "documents"):
        command = commands.add_parser(name)
        command.add_argument("--contains", help="文字子串，包含关联人物名称和别名")
        command.add_argument("--limit", type=int, default=20)
        if name != "documents":
            command.add_argument("--person-account", help="只选原生 exact_account 身份")
    args = parser.parse_args(argv)
    if not args.dh_knowledge and not args.rpa_memory and not args.assistant_db:
        parser.error("请用 --dh-knowledge、--rpa-memory 或 --assistant-db 选择知识来源")
    reader = combine_knowledge(
        *(open_dh_knowledge(path) for path in args.dh_knowledge),
        *(open_rpa_knowledge(path, account_id=args.account) for path in args.rpa_memory),
        *(open_assistant_knowledge(path) for path in args.assistant_db),
    )
    if args.command == "summary":
        result = reader.summary()
    else:
        options = dict(query=args.contains, limit=args.limit)
        if args.command != "documents":
            options["account"] = args.person_account
        result = dict(usage="current_snapshot", items=getattr(reader, args.command)(**options))
    print(json.dumps(result, ensure_ascii=False,
                     default=lambda value: asdict(value) if is_dataclass(value) else value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
