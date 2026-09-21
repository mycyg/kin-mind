"""Controlled Codex Computer Use bridge for autonomous exploration.

DeepSeek chooses the semantic next action, but never receives arbitrary JavaScript
or a generic desktop-control primitive. This MCP server translates a small fixed
tool set into the installed ``cua_repl`` service, validates targets before every
action, and writes host-owned receipts. Optional exact control grants are hints
to a separate fixed-profile DeepSeek/high review; every interaction requires
that review, bound to the full snapshot hash, then re-reads the target before
execution. Browser work is confined to tabs created by this process. Native apps
require an exact allowlist and expose observation, click and scroll only;
control-plane native apps are hard-denied.

The bridge intentionally returns accessibility/DOM text only. Screenshots are not
sent to DeepSeek, so this path makes no claim that deepseek-flash processed images.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import httpx
from mcp.server.fastmcp import Context

from eventmem.core.models import now

from .computer import redact, safe_url
from .web_read import _blocked_address

ADAPTER = "kin-computer-use-v1"
MARKER = "__KIN_CUA__"
TURN_META_KEY = "x-codex-turn-metadata"
REQUIRED_BACKEND_TOOLS = frozenset({"js", "turn_ended"})
MAX_STATE = 16000
MAX_INPUT = 500
MAX_ELEMENT_LINE = 4000
ACTION_REVIEW_CATEGORIES = {
    "read", "navigation", "local_reversible", "local_write", "external_send",
    "purchase", "destructive", "credential", "control_plane", "unknown",
}
HOST_REVIEWABLE_CATEGORIES = {"read", "navigation", "local_reversible", "local_write"}
HARD_DENIED_REVIEW_CATEGORIES = ACTION_REVIEW_CATEGORIES - HOST_REVIEWABLE_CATEGORIES
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,300}$")
SENSITIVE_ENV = re.compile(r"(?i)(?:token|secret|password|passwd|api[_-]?key|authorization|credential)")
SENSITIVE_TEXT = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]|"
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)
# These are control-plane bypasses, not guesses about application semantics.
# Messaging and document apps are configurable; Codex/terminal/settings/browser
# native control is not, because it would bypass this adapter's fixed tools.
HARD_DENIED_APPS = {
    "com.apple.systempreferences", "com.apple.Terminal", "com.googlecode.iterm2",
    "com.openai.codex", "com.google.Chrome",
}


def _service_failure_code(value):
    """Classify CUA failures without echoing UI content or backend diagnostics."""
    text = str(value).lower()
    if re.search(r"\b(?:timed?[- ]?out|timeout)\b", text):
        return "computer-use-service-timeout"
    if re.search(r"\b(?:permission|approval|unauthori[sz]ed|forbidden|denied|declined)\b", text):
        return "computer-use-service-permission-denied"
    if re.search(r"\b(?:disconnect(?:ed)?|connection|transport|unavailable)\b", text):
        return "computer-use-service-unavailable"
    if re.search(r"\b(?:invalid|argument|schema|malformed)\b", text):
        return "computer-use-service-invalid-request"
    if re.search(r"\b(?:stale|missing|not found|closed|unowned)[- ]?(?:tab|app|target)?\b", text):
        return "computer-use-service-target-unavailable"
    return "computer-use-service-error"


class DeepSeekActionReviewer:
    """Independent high-reasoning classification of one proposed UI action.

    The executing model never controls this gateway's profile, token, schema or
    accepted categories. Any transport/schema ambiguity is a refusal, never an
    authorization fallback.
    """

    REQUIRED = frozenset({
        "decision", "category", "effect", "target", "reason", "snapshot_hash",
        "input_version",
    })

    def __init__(self, config, *, client_factory=httpx.AsyncClient):
        self.config = dict(config or {})
        self.client_factory = client_factory
        parsed = urlparse(str(self.config.get("base_url") or ""))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("computer-action-review-gateway-invalid")
        self.base_url = str(self.config["base_url"]).rstrip("/")
        self.env_key = str(self.config.get("env_key") or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.env_key):
            raise ValueError("computer-action-review-env-key-invalid")
        if self.config.get("model", "deepseek-flash") != "deepseek-flash":
            raise ValueError("computer-action-review-model-invalid")
        if self.config.get("reasoning", "high") != "high":
            raise ValueError("computer-action-review-reasoning-invalid")
        self.timeout = float(self.config.get("timeout_seconds", 60))
        if not 1 <= self.timeout <= 60:
            raise ValueError("computer-action-review-timeout-invalid")

    @staticmethod
    def _output_text(body):
        parts = []
        if isinstance(body.get("output_text"), str):
            parts.append(body["output_text"])
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" \
                        and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        if len(parts) != 1:
            raise RuntimeError("computer-action-review-output-invalid")
        return parts[0]

    async def review(self, context):
        token = os.environ.get(self.env_key)
        if not token:
            raise RuntimeError("computer-action-review-credential-unavailable")
        request = {
            "model": "deepseek-flash", "stream": False, "store": False,
            "input": [{"type": "message", "role": "user", "content": [{
                "type": "input_text", "text": _j(context),
            }]}],
        }
        try:
            async with self.client_factory(timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url + "/responses",
                    headers={"Authorization": "Bearer " + token}, json=request,
                )
        except httpx.HTTPError as error:
            raise RuntimeError("computer-action-review-transport-unavailable") from error
        if response.status_code != 200:
            raise RuntimeError("computer-action-review-http-" + str(response.status_code))
        try:
            body = response.json()
            decision = json.loads(self._output_text(body))
        except (ValueError, TypeError) as error:
            raise RuntimeError("computer-action-review-output-invalid") from error
        if body.get("model") != "deepseek-flash" or body.get("status") not in {None, "completed"}:
            raise RuntimeError("computer-action-review-model-unverified")
        if not isinstance(decision, dict) or set(decision) != self.REQUIRED:
            raise RuntimeError("computer-action-review-schema-invalid")
        if decision.get("decision") not in {"allow", "deny"} \
                or decision.get("category") not in ACTION_REVIEW_CATEGORIES:
            raise RuntimeError("computer-action-review-schema-invalid")
        for key in ("effect", "target", "reason"):
            if not isinstance(decision.get(key), str) or not 1 <= len(decision[key]) <= 500:
                raise RuntimeError("computer-action-review-schema-invalid")
        if decision.get("snapshot_hash") != context["snapshot_hash"] \
                or decision.get("input_version") != context["input_version"]:
            raise RuntimeError("computer-action-review-binding-invalid")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        return decision, {
            "provider": "deepseek", "model": body["model"], "reasoning": "high",
            "request_id": body.get("id"), "usage": usage,
            "usage_status": "reported" if usage else "unknown",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }


def _j(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _backend_environment(config):
    # Values are inherited by name at runtime. They must never be serialized in
    # a per-run config file where a broad computer-read root could expose them.
    if config.get("env") not in (None, {}):
        raise ValueError("computer-use-backend-inline-env-refused")
    names = config.get("env_vars") or []
    if not isinstance(names, list) or any(not isinstance(key, str) for key in names):
        raise TypeError("computer-use-backend-env-invalid")
    if len(names) != len(set(names)):
        raise ValueError("computer-use-backend-env-invalid")
    for key in names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or SENSITIVE_ENV.search(key):
            raise ValueError("computer-use-backend-env-refused")
        if key not in os.environ:
            raise ValueError("computer-use-backend-env-missing:" + key)
    inherited = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
                 if key in os.environ}
    return {**inherited, **{key: os.environ[key] for key in names}}


class CuaBackend:
    """Nested MCP client for the actual ``@oai/cua-repl`` service."""

    def __init__(self, config, *, execution_id, attempt, model="deepseek-flash",
                 allowed_apps=(), allowed_app_actions=("observe",)):
        self.config = config
        self.execution_id = str(execution_id)
        self.attempt = int(attempt)
        self.model = model
        self.allowed_apps = set(allowed_apps)
        self.allowed_app_actions = set(allowed_app_actions)
        self.approvals = []
        self.stack = None
        self.session = None
        # This is a private nested MCP client, not the outer Codex-native CUA
        # client. One unique host correlation scope therefore stays fixed from
        # bootstrap through actions and turn_ended. It is not an authorization
        # identity: sender validation, app policy and elicitation still run in
        # the installed PublicGateway/runtime.
        self.scope_meta = {TURN_META_KEY: _j({
            "session_id": f"kin-exploration:{self.execution_id}",
            "turn_id": f"kin-exploration:{self.execution_id}:attempt-{self.attempt}",
            "model": self.model,
        })}
        self.readiness = None

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters, types
        from mcp.client.stdio import stdio_client

        command = self.config.get("command")
        args = self.config.get("args") or []
        if not isinstance(command, str) or not command or not isinstance(args, list):
            raise ValueError("computer-use-backend-unconfigured")
        self.stack = AsyncExitStack()
        try:
            read, write = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                command=command, args=[str(value) for value in args],
                env=_backend_environment(self.config),
            )))

            async def elicitation(_context, params):
                # Computer Use asks once before binding a native app. The outer host
                # already made the authorization exact; accept only that same low-risk
                # app-state request. Everything else is declined, never delegated to DS.
                raw = params.model_dump(by_alias=True)
                meta = raw.get("_meta") or raw.get("meta") or {}
                tool_params = meta.get("tool_params") or {}
                action = {"get_app_state": "observe", "click": "click", "scroll": "scroll"}.get(
                    meta.get("tool_name")
                )
                allowed = (
                    action in self.allowed_app_actions
                    and meta.get("codex_approval_kind") == "mcp_tool_call"
                    and meta.get("connector_id") == "computer-use"
                    and meta.get("riskLevel") == "low"
                    and tool_params.get("app") in self.allowed_apps
                )
                if allowed:
                    self.approvals.append({
                        "state": "approved", "source": "cua-elicitation",
                        "tool": meta.get("tool_name"), "app": tool_params.get("app"),
                        "risk_level": meta.get("riskLevel"), "approved_at": now(),
                    })
                return types.ElicitResult(action="accept", content={}) \
                    if allowed else types.ElicitResult(action="decline")

            self.session = await self.stack.enter_async_context(
                ClientSession(read, write, elicitation_callback=elicitation)
            )
            await self.session.initialize()
            listed = await self.session.list_tools()
            tool_names = {tool.name for tool in listed.tools}
            if not REQUIRED_BACKEND_TOOLS <= tool_names:
                raise RuntimeError("computer-use-service-tools-missing")
            # cua_repl requires this exact first API call in a fresh runtime.
            await self._call("await cua.getState();", "Initialize controlled Computer Use")
            await self.call_json(
                "globalThis.__kinTabs = new Map(); globalThis.__kinApps = new Map(); "
                f"nodeRepl.write({ _j(MARKER) }+JSON.stringify({{ready:true}}));",
                "Initialize controlled target registry",
            )
            self.readiness = {
                "state": "ready", "protocol": "mcp",
                "tools": sorted(REQUIRED_BACKEND_TOOLS), "bootstrap": "cua.getState",
                "scope": "host-exploration", "proof": "bootstrap-only",
                "operational_observation": False,
            }
            return self
        except BaseException:
            await self.stack.aclose()
            self.stack = None
            self.session = None
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        if self.session is not None:
            try:
                turn = json.loads(self.scope_meta[TURN_META_KEY])
                await self.session.call_tool("turn_ended", {
                    "hook_event_name": "turn_ended",
                    "session_id": turn["session_id"],
                    "turn_id": turn["turn_id"],
                }, meta=self.scope_meta)
            except Exception:  # noqa: BLE001, S110 - shutdown must still close stdio
                pass
        if self.stack is not None:
            await self.stack.aclose()

    async def _call(self, code, title):
        result = await self.session.call_tool("js", {
            "code": code, "title": title[:80], "timeout_ms": 30000,
        }, meta=self.scope_meta)
        text = "\n".join(getattr(item, "text", "") for item in result.content)
        if result.isError:
            raise RuntimeError(_service_failure_code(text))
        return text

    async def call_json(self, code, title):
        text = await self._call(code, title)
        matches = re.findall(re.escape(MARKER) + r"([^\r\n]+)", text)
        if not matches:
            raise RuntimeError("computer-use-service-missing-receipt")
        try:
            value = json.loads(matches[-1])
        except ValueError as error:
            raise RuntimeError("computer-use-service-invalid-receipt") from error
        if not isinstance(value, dict):
            raise TypeError("computer-use-service-invalid-receipt")
        return value


def probe_backend_readiness(config, *, execution_id, attempt, model="deepseek-flash",
                            allowed_apps=(), allowed_app_actions=("observe",),
                            timeout_seconds=45):
    """Prove the nested MCP and its required CUA bootstrap work before model dispatch."""
    timeout_seconds = float(timeout_seconds)
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("computer-use-readiness-timeout-invalid")

    async def probe():
        # The probe is a complete, closed logical turn. Its scope must not be
        # reused by the later operational MCP server after turn_ended.
        async with CuaBackend(
            config, execution_id=f"{execution_id}:readiness-probe", attempt=attempt,
            model=model,
            allowed_apps=allowed_apps, allowed_app_actions=allowed_app_actions,
        ) as backend:
            return dict(backend.readiness)

    return asyncio.run(asyncio.wait_for(probe(), timeout=timeout_seconds))


class ComputerUseController:
    """Policy and receipt layer around a CUA backend."""

    def __init__(self, config, backend, action_reviewer=None):
        self.config = config
        self.backend = backend
        self.execution_id = str(config["execution_id"])
        self.attempt = int(config["attempt"])
        self.ledger = Path(config["ledger"])
        self.lock = threading.Lock()
        self.allowed_hosts = set(config.get("allow_hosts", []))
        self.host_allowlist = set(config.get("host_allowlist", []))
        self.browser = str(config.get("browser") or "iab")
        self.allowed_apps = set(config.get("allowed_apps", []))
        self.allowed_app_actions = set(config.get("allowed_app_actions", ["observe"]))
        self.allowed_browser_effects = set(config.get("allowed_browser_effects", []))
        self.allowed_native_effects = set(config.get("allowed_native_effects", []))
        self.browser_element_grants = self._checked_grant_list(
            config.get("browser_element_grants", []), "browser"
        )
        self.native_element_grants = self._checked_grant_list(
            config.get("native_element_grants", []), "native"
        )
        review_config = config.get("action_review") or {}
        categories = set(review_config.get("allowed_categories", []))
        if not categories <= HOST_REVIEWABLE_CATEGORIES:
            raise ValueError("computer-action-review-category-not-host-authorizable")
        self.allowed_review_categories = categories
        self.local_write_hosts = set(review_config.get("local_write_hosts", []))
        self.local_write_apps = set(review_config.get("local_write_apps", []))
        self.action_reviewer = action_reviewer

    def _write(self, entry):
        with self.lock:
            data = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
            data[entry["evidence_id"]] = entry
            self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.ledger.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.chmod(0o600)
            temporary.replace(self.ledger)
        return entry

    def _record_observation(self, *, tool, locator, title, state, action=None):
        text = redact(str(state))[:MAX_STATE]
        version = hashlib.sha256(text.encode()).hexdigest()
        identifier = hashlib.sha256(
            (self.execution_id + "\0" + str(self.attempt) + "\0" + tool + "\0"
             + locator + "\0" + version).encode()
        ).hexdigest()[:32]
        entry = {
            "id": "computer_" + hashlib.sha256((locator + "\0" + version).encode()).hexdigest()[:32],
            "evidence_id": "computer_" + identifier,
            "execution_id": self.execution_id, "attempt": self.attempt,
            "tool": tool, "tool_call_id": "computer_call_" + identifier,
            "adapter": ADAPTER, "state": "observed", "locator": locator,
            "title": redact(title), "version": version, "observed_at": now(),
            "actor": "unknown", "basis": "observed", "excerpt": text[:2000],
            "action": action,
        }
        self._write(entry)
        return {**entry, "text": text}

    def _record_action(self, *, tool, locator, action):
        timestamp = now()
        identifier = hashlib.sha256(
            (self.execution_id + "\0" + str(self.attempt) + "\0" + tool + "\0"
             + locator + "\0" + timestamp).encode()
        ).hexdigest()[:32]
        return self._write({
            "evidence_id": "action_" + identifier, "execution_id": self.execution_id,
            "attempt": self.attempt, "tool": tool, "adapter": ADAPTER, "state": "acted",
            "locator": locator, "observed_at": timestamp, "action": action,
        })

    def _record_review(self, *, tool, locator, context, decision, model_receipt):
        identifier = hashlib.sha256(
            (self.execution_id + "\0" + str(self.attempt) + "\0" + context["input_version"]
             + "\0" + str(model_receipt.get("request_id"))).encode()
        ).hexdigest()[:32]
        return self._write({
            "evidence_id": "review_" + identifier,
            "execution_id": self.execution_id, "attempt": self.attempt,
            "tool": tool, "adapter": ADAPTER, "state": "reviewed", "locator": locator,
            "snapshot_hash": context["snapshot_hash"],
            "input_version": context["input_version"],
            "element_index": context["candidate"]["element_index"],
            "element_text_hash": context["candidate"]["element_line_hash"],
            "decision": decision, "model_receipt": model_receipt,
            "observed_at": now(),
        })

    @staticmethod
    def _snapshot(state, element_line=None):
        """Bounded review text plus a fence over the complete raw AX state."""
        raw = str(state)
        state_hash = hashlib.sha256(raw.encode()).hexdigest()
        text = redact(raw)
        if len(text) <= MAX_STATE:
            return text, state_hash
        if element_line is None:
            return text[:MAX_STATE], state_hash
        target = redact(str(element_line))
        marker = "\n...[review snapshot truncated; exact target follows]...\n"
        if len(target) > MAX_ELEMENT_LINE or len(marker) + len(target) > MAX_STATE:
            raise ValueError("accessibility-element-line-too-long")
        return text[:MAX_STATE - len(marker) - len(target)] + marker + target, state_hash

    @staticmethod
    def _effect_name(effect):
        effect = str(effect).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", effect):
            raise ValueError("ui-effect-invalid")
        return effect

    async def _authorize_interaction(self, *, kind, action, target, locator, tool, state,
                                     element_index, element_line, expected_text, effect,
                                     operation=None):
        effect = self._effect_name(effect)
        allowed_effects = self.allowed_browser_effects if kind == "browser" \
            else self.allowed_native_effects
        grant_id = None
        if effect in allowed_effects:
            grant_id = self._find_element_grant(
                kind=kind, action=action, target=target, expected_text=expected_text,
                effect=effect, element_index=element_index,
            )
        if self.action_reviewer is None:
            raise ValueError("ui-action-review-unavailable")
        snapshot, snapshot_hash = self._snapshot(state, element_line)
        raw_element_hash = hashlib.sha256(str(element_line).encode()).hexdigest()
        candidate = {
            "kind": kind, "tool": tool, "action": action, "target": target,
            "locator": locator, "element_index": element_index,
            "expected_text": redact(str(expected_text)), "element_line": redact(element_line),
            "element_line_hash": raw_element_hash, "host_grant_id": grant_id,
            "claimed_effect": effect, "operation": operation or {},
        }
        input_version = hashlib.sha256(_j({
            "execution_id": self.execution_id, "attempt": self.attempt,
            "snapshot_hash": snapshot_hash, "candidate": candidate,
        }).encode()).hexdigest()
        context = {
            "schema_version": "kin-computer-action-review-v1",
            "execution_id": self.execution_id, "attempt": self.attempt,
            "snapshot_hash": snapshot_hash, "input_version": input_version,
            "snapshot": snapshot, "candidate": candidate,
            "host_permissions": {
                "allowed_categories": sorted(self.allowed_review_categories),
                "local_write_hosts": sorted(self.local_write_hosts),
                "local_write_apps": sorted(self.local_write_apps),
            },
        }
        try:
            decision, model_receipt = await self.action_reviewer.review(context)
        except Exception as error:
            raise ValueError("ui-action-review-unavailable") from error
        review = self._record_review(
            tool=tool, locator=locator, context=context, decision=decision,
            model_receipt=model_receipt,
        )
        category = decision["category"]
        allowed = decision["decision"] == "allow" and category in self.allowed_review_categories
        if category in HARD_DENIED_REVIEW_CATEGORIES:
            allowed = False
        if category == "local_write":
            scoped = target in (self.local_write_hosts if kind == "browser" else self.local_write_apps)
            allowed = allowed and scoped
        if not allowed:
            raise ValueError("ui-action-review-denied:" + category)
        return {
            "source": "deepseek-action-review", "review_id": review["evidence_id"],
            "category": category, "claimed_effect": effect,
            "snapshot_hash": snapshot_hash, "element_line_hash": raw_element_hash,
            "input_version": input_version,
        }

    def _checked_url(self, value):
        parsed = urlparse(str(value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser-url-scheme-refused")
        if parsed.username or parsed.password:
            raise ValueError("browser-url-credentials-refused")
        if any(re.search(r"(?i)(?:token|secret|password|auth|signature|code|key)", key)
               for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError("browser-sensitive-query-refused")
        if self.host_allowlist and parsed.hostname not in self.host_allowlist:
            raise ValueError("browser-host-not-allowlisted")
        if _blocked_address(parsed.hostname, self.allowed_hosts):
            raise ValueError("browser-address-refused")
        return str(value)

    @staticmethod
    def _checked_tab(tab_id):
        if not SAFE_ID.fullmatch(str(tab_id)):
            raise ValueError("browser-tab-id-invalid")
        return str(tab_id)

    def _checked_app(self, app_id, action):
        app_id = str(app_id)
        if app_id in HARD_DENIED_APPS or app_id not in self.allowed_apps:
            raise ValueError("native-app-not-authorized")
        if action not in self.allowed_app_actions:
            raise ValueError("native-app-action-not-authorized")
        return app_id

    @staticmethod
    def _checked_grant_list(value, kind):
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise TypeError(f"{kind}-element-grants-invalid")
        return value

    @staticmethod
    def _checked_element(state, index, expected_text):
        index = int(index)
        if index < 0 or index > 100000:
            raise ValueError("accessibility-element-index-invalid")
        expected_text = str(expected_text).strip()
        if not expected_text or len(expected_text) > 200:
            raise ValueError("accessibility-element-label-invalid")
        match = re.search(rf"(?m)^\s*{index}\s+[^\n]*$", str(state))
        if not match:
            raise ValueError("accessibility-element-changed")
        line = match.group(0).strip()
        if len(line) > MAX_ELEMENT_LINE:
            raise ValueError("accessibility-element-line-too-long")
        body = re.sub(rf"^{index}\s+", "", line, count=1).strip()
        _role, separator, label = body.partition(" ")
        labels = {body.casefold()}
        if separator:
            label = label.strip()
            if len(label) >= 2 and label[0] == label[-1] and label[0] in {'"', "'"}:
                label = label[1:-1].strip()
            labels.add(label.casefold())
        if expected_text.casefold() not in labels:
            raise ValueError("accessibility-element-changed")
        return index, line

    @staticmethod
    def _grant_id(grant):
        canonical = json.dumps(grant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "grant_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def _find_element_grant(self, *, kind, action, target, expected_text, effect,
                            element_index=None):
        """Resolve an exact host-owned element/effect grant.

        DeepSeek supplies the semantic effect, but cannot create or alter this
        configuration. A matching grant is only a permission hint supplied to
        the independent reviewer; it never bypasses review or the fresh fence.
        """
        grants = self.browser_element_grants if kind == "browser" else self.native_element_grants
        target_key = "host" if kind == "browser" else "app_id"
        label = str(expected_text).strip()
        for grant in grants:
            if (
                grant.get("action") == action
                and grant.get(target_key) == target
                and grant.get("effect") == effect
                and str(grant.get("expected_text") or "").strip() == label
                and ("element_index" not in grant or int(grant["element_index"]) == element_index)
            ):
                return self._grant_id(grant)
        return None

    def _native_approval(self, tool, app_id):
        expected = {"observe_native_app": "get_app_state", "click_native_element": "click",
                    "scroll_native_app": "scroll"}.get(tool)
        approvals = getattr(self.backend, "approvals", [])
        return next((value for value in reversed(approvals)
                     if value.get("tool") == expected and value.get("app") == app_id), None)

    async def _tab_state(self, tab_id):
        tab_id = self._checked_tab(tab_id)
        return await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); "
            "let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||'',title:kinInfo.title||'',state:kinState}));",
            "Read controlled browser tab",
        )

    async def open_browser_page(self, url):
        url = self._checked_url(url)
        if not SAFE_ID.fullmatch(self.browser):
            raise ValueError("browser-id-invalid")
        options = {"sessionName": "🔎 Kin exploration"}
        if self.browser == "iab":
            options["visible"] = False
        value = await self.backend.call_json(
            "let kinTab = await cua.createBrowserTab(" + _j(self.browser) + "," + _j(url)
            + "," + _j(options) + "); "
            "globalThis.__kinTabs.set(kinTab.id,kinTab); "
            "let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||" + _j(url)
            + ",title:kinInfo.title||'',state:kinState}));",
            "Open controlled browser page",
        )
        locator = self._checked_url(value.get("url") or url)
        return {"tab_id": value["tab_id"], **self._record_observation(
            tool="open_browser_page", locator=locator, title=value.get("title") or safe_url(locator),
            state=value.get("state", ""), action={"kind": "open-new-tab", "reversible": True},
        )}

    async def read_browser_page(self, tab_id):
        value = await self._tab_state(tab_id)
        locator = self._checked_url(value.get("url"))
        return {"tab_id": value["tab_id"], **self._record_observation(
            tool="read_browser_page", locator=locator, title=value.get("title") or safe_url(locator),
            state=value.get("state", ""),
        )}

    async def navigate_browser_page(self, tab_id, url):
        tab_id, url = self._checked_tab(tab_id), self._checked_url(url)
        value = await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); await kinTab.goto(" + _j(url) + "); "
            "let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||" + _j(url)
            + ",title:kinInfo.title||'',state:kinState}));",
            "Navigate controlled browser tab",
        )
        locator = self._checked_url(value.get("url") or url)
        return {"tab_id": value["tab_id"], **self._record_observation(
            tool="navigate_browser_page", locator=locator, title=value.get("title") or safe_url(locator),
            state=value.get("state", ""), action={"kind": "navigate", "reversible": True},
        )}

    async def click_browser_element(self, tab_id, element_index, expected_text, effect):
        if not self.config.get("allow_browser_click", False):
            raise ValueError("browser-click-disabled")
        effect = self._effect_name(effect)
        tab_id = self._checked_tab(tab_id)
        before = await self._tab_state(tab_id)
        before_url = self._checked_url(before.get("url"))
        index, line = self._checked_element(before.get("state"), element_index, expected_text)
        authorization = await self._authorize_interaction(
            kind="browser", action="click", target=urlparse(before_url).hostname,
            locator=before_url, tool="click_browser_element", state=before.get("state"),
            element_index=index, element_line=line, expected_text=expected_text, effect=effect,
        )
        fresh = await self._tab_state(tab_id)
        index, fresh_line = self._checked_element(
            fresh.get("state"), element_index, expected_text
        )
        if self._checked_url(fresh.get("url")) != before_url \
                or self._snapshot(fresh.get("state"))[1] != authorization["snapshot_hash"] \
                or hashlib.sha256(fresh_line.encode()).hexdigest() \
                != authorization["element_line_hash"]:
            raise ValueError("ui-snapshot-changed-after-review")
        value = await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); await kinTab.click(" + str(index) + "); "
            "let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||'',title:kinInfo.title||'',state:kinState}));",
            "Click non-impactful browser control",
        )
        locator = self._checked_url(value.get("url") or before.get("url"))
        return {"tab_id": value["tab_id"], **self._record_observation(
            tool="click_browser_element", locator=locator,
            title=value.get("title") or safe_url(locator), state=value.get("state", ""),
            action={"kind": "click", "element_index": index,
                    "expected_text": redact(expected_text), "declared_effect": effect,
                    "authorization": authorization},
        )}

    async def type_browser_text(self, tab_id, element_index, expected_text, text, effect):
        if not self.config.get("allow_browser_text", False):
            raise ValueError("browser-text-entry-disabled")
        effect = self._effect_name(effect)
        text = str(text)
        if not text or len(text) > MAX_INPUT or SENSITIVE_TEXT.search(text):
            raise ValueError("browser-text-refused")
        tab_id = self._checked_tab(tab_id)
        before = await self._tab_state(tab_id)
        before_url = self._checked_url(before.get("url"))
        index, line = self._checked_element(before.get("state"), element_index, expected_text)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        authorization = await self._authorize_interaction(
            kind="browser", action="type", target=urlparse(before_url).hostname,
            locator=before_url, tool="type_browser_text", state=before.get("state"),
            element_index=index, element_line=line, expected_text=expected_text, effect=effect,
            operation={"characters": len(text), "text_hash": text_hash, "text": redact(text)},
        )
        fresh = await self._tab_state(tab_id)
        index, fresh_line = self._checked_element(
            fresh.get("state"), element_index, expected_text
        )
        if self._checked_url(fresh.get("url")) != before_url \
                or self._snapshot(fresh.get("state"))[1] != authorization["snapshot_hash"] \
                or hashlib.sha256(fresh_line.encode()).hexdigest() \
                != authorization["element_line_hash"]:
            raise ValueError("ui-snapshot-changed-after-review")
        value = await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); await kinTab.setValue(" + str(index)
            + "," + _j(text) + "); let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||'',title:kinInfo.title||'',state:kinState}));",
            "Enter bounded non-sensitive browser text",
        )
        locator = self._checked_url(value.get("url") or before.get("url"))
        return {"tab_id": value["tab_id"], **self._record_observation(
            tool="type_browser_text", locator=locator,
            title=value.get("title") or safe_url(locator), state=value.get("state", ""),
            action={"kind": "set-value", "element_index": index, "characters": len(text),
                    "text_hash": text_hash, "declared_effect": effect,
                    "authorization": authorization},
        )}

    async def close_browser_page(self, tab_id):
        tab_id = self._checked_tab(tab_id)
        await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); await kinTab.close(); "
            "globalThis.__kinTabs.delete(" + _j(tab_id) + "); nodeRepl.write("
            + _j(MARKER) + "+JSON.stringify({closed:true,tab_id:" + _j(tab_id) + "}));",
            "Close controlled browser tab",
        )
        receipt = self._record_action(tool="close_browser_page", locator="browser-tab://" + tab_id,
                                      action={"kind": "close-created-tab", "reversible": False})
        return {"state": "closed", "tab_id": tab_id, "receipt_id": receipt["evidence_id"]}

    async def _native_state(self, app_id):
        value = await self.backend.call_json(
            "let kinApp = await cua.getApp(" + _j(app_id) + "); globalThis.__kinApps.set("
            + _j(app_id) + ",kinApp); let kinState = await kinApp.getAXState({emit:false,disableDiffing:true}); "
            "nodeRepl.write(" + _j(MARKER) + "+JSON.stringify({state:kinState}));",
            "Observe authorized native app",
        )
        return str(value.get("state", ""))

    def _record_native_state(self, app_id, state):
        return self._record_observation(
            tool="observe_native_app", locator="computer://app/" + app_id,
            title=app_id, state=state,
            action={"kind": "observe", "approval": self._native_approval(
                "observe_native_app", app_id)},
        )

    async def observe_native_app(self, app_id):
        app_id = self._checked_app(app_id, "observe")
        return self._record_native_state(app_id, await self._native_state(app_id))

    async def click_native_element(self, app_id, element_index, expected_text, effect):
        app_id = self._checked_app(app_id, "click")
        effect = self._effect_name(effect)
        before_state = await self._native_state(app_id)
        self._record_native_state(app_id, before_state)
        index, line = self._checked_element(before_state, element_index, expected_text)
        locator = "computer://app/" + app_id
        authorization = await self._authorize_interaction(
            kind="native", action="click", target=app_id, locator=locator,
            tool="click_native_element", state=before_state, element_index=index,
            element_line=line, expected_text=expected_text, effect=effect,
        )
        fresh_state = await self._native_state(app_id)
        self._record_native_state(app_id, fresh_state)
        index, fresh_line = self._checked_element(fresh_state, element_index, expected_text)
        if self._snapshot(fresh_state)[1] != authorization["snapshot_hash"] \
                or hashlib.sha256(fresh_line.encode()).hexdigest() \
                != authorization["element_line_hash"]:
            raise ValueError("ui-snapshot-changed-after-review")
        value = await self.backend.call_json(
            "let kinApp = globalThis.__kinApps.get(" + _j(app_id) + "); "
            "if (!kinApp) throw new Error('unowned-app'); await kinApp.click(" + str(index) + "); "
            "let kinState = await kinApp.getAXState({emit:false,disableDiffing:true}); "
            "nodeRepl.write(" + _j(MARKER) + "+JSON.stringify({state:kinState}));",
            "Click reversible authorized app control",
        )
        return self._record_observation(
            tool="click_native_element", locator=locator, title=app_id,
            state=value.get("state", ""), action={"kind": "click", "element_index": index,
                                                  "reversible": True,
                                                  "expected_text": redact(expected_text),
                                                  "declared_effect": effect,
                                                  "authorization": authorization,
                                                  "approval": self._native_approval(
                                                      "click_native_element", app_id)},
        )

    async def scroll_native_app(self, app_id, element_index, expected_text, effect,
                                direction="down", pages=1):
        app_id = self._checked_app(app_id, "scroll")
        effect = self._effect_name(effect)
        if direction not in {"up", "down", "left", "right"} or not 1 <= int(pages) <= 3:
            raise ValueError("native-scroll-invalid")
        before_state = await self._native_state(app_id)
        self._record_native_state(app_id, before_state)
        index, line = self._checked_element(before_state, element_index, expected_text)
        locator = "computer://app/" + app_id
        authorization = await self._authorize_interaction(
            kind="native", action="scroll", target=app_id, locator=locator,
            tool="scroll_native_app", state=before_state, element_index=index,
            element_line=line, expected_text=expected_text, effect=effect,
            operation={"direction": direction, "pages": int(pages)},
        )
        fresh_state = await self._native_state(app_id)
        self._record_native_state(app_id, fresh_state)
        index, fresh_line = self._checked_element(fresh_state, element_index, expected_text)
        if self._snapshot(fresh_state)[1] != authorization["snapshot_hash"] \
                or hashlib.sha256(fresh_line.encode()).hexdigest() \
                != authorization["element_line_hash"]:
            raise ValueError("ui-snapshot-changed-after-review")
        value = await self.backend.call_json(
            "let kinApp = globalThis.__kinApps.get(" + _j(app_id) + "); "
            "if (!kinApp) throw new Error('unowned-app'); await kinApp.scroll(" + str(index) + ","
            + _j(direction) + "," + str(int(pages)) + "); "
            "let kinState = await kinApp.getAXState({emit:false,disableDiffing:true}); "
            "nodeRepl.write(" + _j(MARKER) + "+JSON.stringify({state:kinState}));",
            "Scroll authorized native app",
        )
        return self._record_observation(
            tool="scroll_native_app", locator=locator, title=app_id,
            state=value.get("state", ""), action={"kind": "scroll", "element_index": index,
                                                  "direction": direction, "pages": int(pages),
                                                  "declared_effect": effect,
                                                  "expected_text": redact(expected_text),
                                                  "authorization": authorization,
                                                  "approval": self._native_approval(
                                                      "scroll_native_app", app_id)},
        )


def create_server(config):
    from mcp.server.fastmcp import FastMCP

    @asynccontextmanager
    async def lifespan(_server):
        backend_config = config.get("backend") or {}
        review_config = config.get("action_review") or {}
        reviewer = DeepSeekActionReviewer(review_config) if review_config.get("enabled") else None
        async with CuaBackend(
            backend_config, execution_id=config["execution_id"], attempt=config["attempt"],
            model=config.get("model") or "deepseek-flash", allowed_apps=config.get("allowed_apps", []),
            allowed_app_actions=config.get("allowed_app_actions", ["observe"]),
        ) as backend:
            yield ComputerUseController(config, backend, reviewer)

    server = FastMCP("kin_ui", lifespan=lifespan)

    def controller(ctx):
        return ctx.request_context.lifespan_context

    @server.tool()
    async def open_browser_page(url: str, ctx: Context) -> dict:
        """在受控新标签页中打开已核验 URL，返回当前 AX/DOM 文本。"""
        return await controller(ctx).open_browser_page(url)

    @server.tool()
    async def read_browser_page(tab_id: str, ctx: Context) -> dict:
        """读取本次探索创建的标签页的当前 AX/DOM 文本。"""
        return await controller(ctx).read_browser_page(tab_id)

    @server.tool()
    async def navigate_browser_page(tab_id: str, url: str, ctx: Context) -> dict:
        """把受控标签页导航到经过校验的公开或明确允许的 URL。"""
        return await controller(ctx).navigate_browser_page(tab_id, url)

    @server.tool()
    async def click_browser_element(tab_id: str, element_index: int, expected_text: str,
                                    effect: str, ctx: Context) -> dict:
        """复核并点击本次探索标签页中的当前 AX 元素。expected_text 原样复制最新 AX 行中数字编号后的完整文字，缩短文字会因目标陈旧或含糊而被拒绝。"""
        return await controller(ctx).click_browser_element(tab_id, element_index, expected_text, effect)

    @server.tool()
    async def type_browser_text(tab_id: str, element_index: int, expected_text: str,
                                text: str, effect: str, ctx: Context) -> dict:
        """宿主已允许文字输入时，填写非敏感文本。expected_text 原样复制最新 AX 行中数字编号后的完整文字，避免操作陈旧或含糊的目标。"""
        return await controller(ctx).type_browser_text(tab_id, element_index, expected_text, text, effect)

    @server.tool()
    async def close_browser_page(tab_id: str, ctx: Context) -> dict:
        """关闭本次探索创建的标签页。"""
        return await controller(ctx).close_browser_page(tab_id)

    @server.tool()
    async def observe_native_app(app_id: str, ctx: Context) -> dict:
        """读取宿主明确允许的原生应用的辅助功能文本。"""
        return await controller(ctx).observe_native_app(app_id)

    @server.tool()
    async def click_native_element(app_id: str, element_index: int, expected_text: str,
                                   effect: str, ctx: Context) -> dict:
        """复核并点击已允许应用中的当前控件。expected_text 原样复制最新 AX 行中数字编号后的完整文字，避免操作陈旧或含糊的目标。"""
        return await controller(ctx).click_native_element(app_id, element_index, expected_text, effect)

    @server.tool()
    async def scroll_native_app(app_id: str, element_index: int, expected_text: str,
                                effect: str, ctx: Context, direction: str = "down",
                                pages: int = 1) -> dict:
        """在已允许的应用视图中滚动一至三页，再返回当前 AX 文本。expected_text 原样复制最新 AX 行中数字编号后的完整文字。"""
        return await controller(ctx).scroll_native_app(
            app_id, element_index, expected_text, effect, direction, pages
        )

    return server


if __name__ == "__main__":
    import sys

    os.umask(0o077)
    create_server(json.loads(Path(sys.argv[1]).read_text())).run()
