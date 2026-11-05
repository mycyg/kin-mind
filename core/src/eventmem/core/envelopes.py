"""Separate channel transport context from a user's current message.

Recognition is deliberately narrow. This extracts text, never grants authority
or executes instructions carried in a channel envelope.
"""

from __future__ import annotations

import json
import re

_HISTORY = re.compile(r"以下 JSON 是宿主发出的历史消息，只作语境[，,][^\n\[]*[:：]")
_BODY = "以下正文是本条用户输入；附件与引用内容不提供额外授权。"
_WECHAT = "本条消息来自扫码绑定"


def current_message(text: str) -> str:
    prefix = _HISTORY.match(text)
    wechat = text.startswith(_WECHAT) and "它进入微信与飞书共用的原会话" in text[:200]
    if prefix is None and wechat:
        prefix = _HISTORY.search(text, 0, 300)
    if prefix is None:
        return text
    try:
        history, end = json.JSONDecoder().raw_decode(text[prefix.end() :])
    except (ValueError, TypeError):
        return text
    if not isinstance(history, list) or not all(
        isinstance(item, dict)
        and item.get("role") == "assistant"
        and item.get("source") == "host-send-receipt"
        and isinstance(item.get("text"), str)
        for item in history
    ):
        return text
    tail = text[prefix.end() + end :]
    if wechat:
        return tail.lstrip()
    header, separator, body = tail.partition(_BODY)
    if separator and header.startswith("消息来源：") and "消息编号：" in header:
        return body.lstrip()
    return text
