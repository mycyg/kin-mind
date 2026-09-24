"""Controlled Codex Computer Use bridge for autonomous exploration.

The executor never receives arbitrary JavaScript or a generic desktop-control
primitive. This MCP server translates a small fixed tool set into the installed
``cua_repl`` service, validates targets before every action, and writes
host-owned receipts. Reading and navigating need no review; an interaction is
declared by the executor as one category and the host applies the owner's hard
rules to it: sending or publishing, payment, deletion, credentials and system
control are refused, a local write needs a host-scoped target, and a target the
host cannot place is handed back with its reason instead of being guessed at.
No second model judges an interaction. Browser work is confined to tabs created
by this process. Native apps require an exact allowlist and expose observation,
click and scroll only; control-plane native apps are hard-denied.

The bridge intentionally returns accessibility/DOM text only. Screenshots are not
sent to the executor's model, so this path makes no claim that it processed images.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from mcp.server.fastmcp import Context

from eventmem.core.models import now

from .computer import redact, safe_url
from .web_read import public_host_refusal

ADAPTER = "kin-computer-use-v1"
MARKER = "__KIN_CUA__"
TURN_META_KEY = "x-codex-turn-metadata"
REQUIRED_BACKEND_TOOLS = frozenset({"js", "turn_ended"})
MAX_STATE = 16000
MAX_INPUT = 500
MAX_ELEMENT_LINE = 4000
# What an interaction may be declared as. The first three need nothing from the host but a
# current target; a local write needs a target the host scoped for writing; the rest are the
# owner's hard rules and are refused whatever else is said about them.
ACTION_CATEGORIES = ("read", "navigation", "local_reversible", "local_write", "external_send",
                     "purchase", "destructive", "credential", "control_plane")
OPEN_CATEGORIES = frozenset({"read", "navigation", "local_reversible"})
HARD_DENIED_CATEGORIES = frozenset({"external_send", "purchase", "destructive", "credential", "control_plane"})
# A target whose own label names one of the hard rules is not placed by a declaration: the
# host cannot tell a "Pay" button that only opens a page from one that pays, so it hands the
# action back with the reason and the executor chooses another way.
HARD_RULE_SIGNALS = {
    "purchase": re.compile(r"(?i)支付|付款|购买|下单|结算|结账|充值|订阅|"
                           r"\b(?:pay|buy|purchase|checkout|check out|place order|order now|subscribe)\b"),
    "external_send": re.compile(r"(?i)发送|发布|发表|提交|转发|分享|"
                                r"\b(?:send|post|publish|submit|tweet|retweet|share)\b"),
    "destructive": re.compile(r"(?i)删除|移除|清空|注销|废纸篓|"
                              r"\b(?:delete|remove|erase|destroy|empty trash|move to trash|deactivate|reset)\b"),
    "credential": re.compile(r"(?i)密码|安全设置|隐私设置|权限|授权|双重验证|"
                             r"\b(?:password|security settings|privacy settings|permissions?|authori[sz]e|two-factor|2fa|api key)\b"),
}
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

    def __init__(self, config, backend):
        self.config = config
        self.backend = backend
        self.execution_id = str(config["execution_id"])
        self.attempt = int(config["attempt"])
        self.ledger = Path(config["ledger"])
        self.lock = threading.Lock()
        self.allowed_hosts = set(config.get("allow_hosts", []))
        self.host_allowlist = set(config.get("host_allowlist", []))
        self.public_transport = config.get("public_transport")
        self.browser = str(config.get("browser") or "iab")
        self.allowed_apps = set(config.get("allowed_apps", []))
        self.allowed_app_actions = set(config.get("allowed_app_actions", ["observe"]))
        self.local_write_hosts = set(config.get("local_write_hosts", []))
        self.local_write_apps = set(config.get("local_write_apps", []))

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
             + locator + "\0" + timestamp + "\0" + _j(action)).encode()
        ).hexdigest()[:32]
        return self._write({
            "evidence_id": "action_" + identifier, "execution_id": self.execution_id,
            "attempt": self.attempt, "tool": tool, "adapter": ADAPTER, "state": "acted",
            "locator": locator, "observed_at": timestamp, "action": action,
        })

    def _acted(self, *, tool, locator, title, state, action):
        """An interaction leaves two receipts: what was done, and what the target showed after."""
        receipt = self._record_action(tool=tool, locator=locator, action=action)
        return {**self._record_observation(tool=tool, locator=locator, title=title, state=state,
                                           action=action), "action_receipt_id": receipt["evidence_id"]}

    @staticmethod
    def _category(effect):
        """The declared category of an interaction; `category:detail` keeps a short note."""
        effect = str(effect).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}(?::[a-z0-9_-]{1,63})?", effect):
            raise ValueError("ui-effect-invalid")
        category = effect.split(":", 1)[0]
        if category in HARD_DENIED_CATEGORIES:
            raise ValueError("ui-action-hard-denied:" + category)
        if category not in ACTION_CATEGORIES:
            # Not a category the host knows: handed back so the executor says what it means.
            raise ValueError("ui-action-uncertain:undeclared-category")
        return effect, category

    def _authorize_interaction(self, *, kind, target, element_line, expected_text, effect):
        """The owner's hard rules, applied by the host alone. Nothing here asks another model."""
        effect, category = self._category(effect)
        label = " ".join((str(expected_text), str(element_line)))
        signal = next((name for name, pattern in HARD_RULE_SIGNALS.items() if pattern.search(label)), None)
        if signal:
            raise ValueError("ui-action-uncertain:" + signal)
        if category == "local_write" and target not in (
                self.local_write_hosts if kind == "browser" else self.local_write_apps):
            raise ValueError("ui-action-not-authorized:local_write")
        return {"source": "host-policy", "category": category, "declared_effect": effect,
                "element_line_hash": hashlib.sha256(str(element_line).encode()).hexdigest()}

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
        # The same public-address check the web reader makes, through the same resolver, so a
        # Fake-IP network (whose local answers are private) refuses nothing that is public.
        if public_host_refusal(parsed.hostname, self.allowed_hosts, self.public_transport):
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
        tab_id = self._checked_tab(tab_id)
        before = await self._tab_state(tab_id)
        before_url = self._checked_url(before.get("url"))
        index, line = self._checked_element(before.get("state"), element_index, expected_text)
        authorization = self._authorize_interaction(
            kind="browser", target=urlparse(before_url).hostname, element_line=line,
            expected_text=expected_text, effect=effect)
        value = await self.backend.call_json(
            "let kinTab = globalThis.__kinTabs.get(" + _j(tab_id) + "); "
            "if (!kinTab) throw new Error('unowned-tab'); await kinTab.click(" + str(index) + "); "
            "let kinState = await kinTab.getAXState({emit:false,disableDiffing:true}); "
            "let kinInfo = (await cua.listTabs({browser:" + _j(self.browser)
            + ",emit:false})).find((v)=>v.id===kinTab.id)||{}; "
            "nodeRepl.write(" + _j(MARKER)
            + "+JSON.stringify({tab_id:kinTab.id,url:kinInfo.url||'',title:kinInfo.title||'',state:kinState}));",
            "Click browser control",
        )
        locator = self._checked_url(value.get("url") or before.get("url"))
        return {"tab_id": value["tab_id"], **self._acted(
            tool="click_browser_element", locator=locator,
            title=value.get("title") or safe_url(locator), state=value.get("state", ""),
            action={"kind": "click", "element_index": index, "expected_text": redact(expected_text),
                    "authorization": authorization},
        )}

    async def type_browser_text(self, tab_id, element_index, expected_text, text, effect):
        if not self.config.get("allow_browser_text", False):
            raise ValueError("browser-text-entry-disabled")
        text = str(text)
        if not text or len(text) > MAX_INPUT or SENSITIVE_TEXT.search(text):
            raise ValueError("browser-text-refused")
        tab_id = self._checked_tab(tab_id)
        before = await self._tab_state(tab_id)
        before_url = self._checked_url(before.get("url"))
        index, line = self._checked_element(before.get("state"), element_index, expected_text)
        authorization = self._authorize_interaction(
            kind="browser", target=urlparse(before_url).hostname, element_line=line,
            expected_text=expected_text, effect=effect)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
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
        return {"tab_id": value["tab_id"], **self._acted(
            tool="type_browser_text", locator=locator,
            title=value.get("title") or safe_url(locator), state=value.get("state", ""),
            action={"kind": "set-value", "element_index": index, "characters": len(text),
                    "text_hash": text_hash, "authorization": authorization},
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
        before_state = await self._native_state(app_id)
        self._record_native_state(app_id, before_state)
        index, line = self._checked_element(before_state, element_index, expected_text)
        locator = "computer://app/" + app_id
        authorization = self._authorize_interaction(
            kind="native", target=app_id, element_line=line, expected_text=expected_text, effect=effect)
        value = await self.backend.call_json(
            "let kinApp = globalThis.__kinApps.get(" + _j(app_id) + "); "
            "if (!kinApp) throw new Error('unowned-app'); await kinApp.click(" + str(index) + "); "
            "let kinState = await kinApp.getAXState({emit:false,disableDiffing:true}); "
            "nodeRepl.write(" + _j(MARKER) + "+JSON.stringify({state:kinState}));",
            "Click authorized app control",
        )
        return self._acted(
            tool="click_native_element", locator=locator, title=app_id,
            state=value.get("state", ""), action={"kind": "click", "element_index": index,
                                                  "expected_text": redact(expected_text),
                                                  "authorization": authorization,
                                                  "approval": self._native_approval(
                                                      "click_native_element", app_id)},
        )

    async def scroll_native_app(self, app_id, element_index, expected_text, effect,
                                direction="down", pages=1):
        app_id = self._checked_app(app_id, "scroll")
        if direction not in {"up", "down", "left", "right"} or not 1 <= int(pages) <= 3:
            raise ValueError("native-scroll-invalid")
        before_state = await self._native_state(app_id)
        self._record_native_state(app_id, before_state)
        index, line = self._checked_element(before_state, element_index, expected_text)
        locator = "computer://app/" + app_id
        authorization = self._authorize_interaction(
            kind="native", target=app_id, element_line=line, expected_text=expected_text, effect=effect)
        value = await self.backend.call_json(
            "let kinApp = globalThis.__kinApps.get(" + _j(app_id) + "); "
            "if (!kinApp) throw new Error('unowned-app'); await kinApp.scroll(" + str(index) + ","
            + _j(direction) + "," + str(int(pages)) + "); "
            "let kinState = await kinApp.getAXState({emit:false,disableDiffing:true}); "
            "nodeRepl.write(" + _j(MARKER) + "+JSON.stringify({state:kinState}));",
            "Scroll authorized native app",
        )
        return self._acted(
            tool="scroll_native_app", locator=locator, title=app_id,
            state=value.get("state", ""), action={"kind": "scroll", "element_index": index,
                                                  "direction": direction, "pages": int(pages),
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
        async with CuaBackend(
            backend_config, execution_id=config["execution_id"], attempt=config["attempt"],
            model=config.get("model") or "deepseek-flash", allowed_apps=config.get("allowed_apps", []),
            allowed_app_actions=config.get("allowed_app_actions", ["observe"]),
        ) as backend:
            yield ComputerUseController(config, backend)

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
        """点击本次探索标签页中的当前 AX 元素。expected_text 原样复制最新 AX 行中数字编号后的完整文字，缩短文字会因目标陈旧或含糊而被拒绝。effect 写这次操作的类别：read、navigation、local_reversible，或宿主已为该目标开放的 local_write，可加冒号与简短说明，例如 local_reversible:expand-menu。外部发送、付款、删除、凭据与系统控制一律拒绝；宿主看不准的目标会带原因退回，换一种做法即可。"""
        return await controller(ctx).click_browser_element(tab_id, element_index, expected_text, effect)

    @server.tool()
    async def type_browser_text(tab_id: str, element_index: int, expected_text: str,
                                text: str, effect: str, ctx: Context) -> dict:
        """宿主已允许文字输入时，填写非敏感文本；填写本身不提交。expected_text 原样复制最新 AX 行中数字编号后的完整文字，避免操作陈旧或含糊的目标。effect 写这次操作的类别：read、navigation、local_reversible，或宿主已为该目标开放的 local_write，可加冒号与简短说明，例如 local_reversible:expand-menu。外部发送、付款、删除、凭据与系统控制一律拒绝；宿主看不准的目标会带原因退回，换一种做法即可。"""
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
        """点击已允许应用中的当前控件。expected_text 原样复制最新 AX 行中数字编号后的完整文字，避免操作陈旧或含糊的目标。effect 写这次操作的类别：read、navigation、local_reversible，或宿主已为该目标开放的 local_write，可加冒号与简短说明，例如 local_reversible:expand-menu。外部发送、付款、删除、凭据与系统控制一律拒绝；宿主看不准的目标会带原因退回，换一种做法即可。"""
        return await controller(ctx).click_native_element(app_id, element_index, expected_text, effect)

    @server.tool()
    async def scroll_native_app(app_id: str, element_index: int, expected_text: str,
                                effect: str, ctx: Context, direction: str = "down",
                                pages: int = 1) -> dict:
        """在已允许的应用视图中滚动一至三页，再返回当前 AX 文本。expected_text 原样复制最新 AX 行中数字编号后的完整文字。effect 写这次操作的类别：read、navigation、local_reversible，或宿主已为该目标开放的 local_write，可加冒号与简短说明，例如 local_reversible:expand-menu。外部发送、付款、删除、凭据与系统控制一律拒绝；宿主看不准的目标会带原因退回，换一种做法即可。"""
        return await controller(ctx).scroll_native_app(
            app_id, element_index, expected_text, effect, direction, pages
        )

    return server


if __name__ == "__main__":
    import sys

    os.umask(0o077)
    create_server(json.loads(Path(sys.argv[1]).read_text())).run()
