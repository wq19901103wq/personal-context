"""Project shared history into DH's existing live observed-input format."""

from common.history import HistoryMessage, HistoryReader


def _text(message: HistoryMessage) -> str:
    if message.message_type == "text":
        return message.content or ""
    label = {"image": "图片", "sticker": "表情包", "mixed": "图文",
             "link_card": "链接卡片", "video": "视频"}.get(message.message_type, message.message_type)
    return "\n".join(part for part in (
        f"[{label}]", message.image_description, message.content,
    ) if part)


def recent_draft_input(history: HistoryReader, chat_id: str, *, limit: int = 50) -> dict:
    chat = history.get_chat(chat_id)
    context = []
    for message in history.recent(chat_id, limit=limit):
        text = _text(message)
        if not text.strip() or message.sender.is_self is None:
            continue
        observed_at = message.time.first_observed_at
        context.append({
            "text": text,
            "sender": message.sender.display_name or message.sender.source_sender_id or "unknown",
            "is_self": message.sender.is_self,
            "timestamp": observed_at if observed_at is not None else message.time.recorded_at,
            "time_basis": "first_observed" if observed_at is not None else "recorded",
        })
    context.sort(key=lambda message: message["timestamp"])
    return {"kind": "observed", "chat_type": chat.chat_type,
            "chat_name": chat.display_name, "context": context}


def draft_input_from_args(args):
    from .connection import history_from_args

    return recent_draft_input(history_from_args(args), args.history_chat, limit=args.history_limit)
