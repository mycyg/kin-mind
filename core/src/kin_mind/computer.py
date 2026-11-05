"""Secrets out of text: what `redact` leaves of a value is safe to keep in a receipt or a log."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_VALUE = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|authorization|client[_-]?secret)\b[\s\"']*[:=][\s\"']*)([^\s\"',;}]+)")
TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|Bearer\s+[A-Za-z0-9._~+/-]{12,})", re.IGNORECASE)
# What the two rules above miss (E1-06), applied with them by `redact` as one rule set: a secret
# under a plain `token`/`secret`/`authorization` key with its scheme (`Basic ...`), GitHub
# fine-grained tokens, Google API keys, and the Chinese words for a password or key.
SECRET_MORE = re.compile(r"(?i)(\b(?:token|secret|authorization)\b[\s\"']*[:=][\s\"']*)(?:(?:basic|bearer|token)\s+)?([^\s\"',;}]+)")
TOKEN_MORE = re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,})")
SECRET_WORD = re.compile(r"((?:密码|口令|密钥|令牌)\s*[:：=]\s*)(\S+)")


def redact(value):
    if isinstance(value, str):
        value = re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m.group()), value)
        value = SECRET_VALUE.sub(r"\1[redacted]", SECRET_MORE.sub(r"\1[redacted]", value))
        return TOKEN.sub("[redacted]", TOKEN_MORE.sub("[redacted]", SECRET_WORD.sub(r"\1[redacted]", value)))
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: "[redacted]" if re.fullmatch(r"(?i)(password|secret|token|authorization|api_key)", k) else redact(v) for k, v in value.items()}
    return value


def safe_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += ":" + str(parts.port)
    except ValueError:
        return "[unparseable-url]"
    query = [(k, "[redacted]" if re.search(r"token|key|secret|password|auth|signature|code", k, re.IGNORECASE) else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return TOKEN.sub("[redacted]", SECRET_VALUE.sub(r"\1[redacted]", urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))))
