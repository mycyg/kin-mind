"""Versioned project prompt text. A save replaces one document atomically."""
from pathlib import Path
import json
import os
import uuid
import fcntl
from datetime import datetime, timezone

MAX_PREVIOUS_TEXTS = 16
# Names a prompt had before it was renamed. Tool search has no name, so its description was kept as
# `tools/undefined` until the gateway named nameless tools by their type (2026-10-26).
RENAMED = {'tools/undefined': 'tools/tool_search'}


def assembled(name):
    """System texts assembled per call from their context: an edit freezes the one variant it saw."""
    parts = name.split('/')
    return len(parts) == 2 and parts[0] in ('assessment', 'provider')


def edited(value):
    """A document that overrides its code default; `edited: false` follows the default again."""
    return bool(value) and (value.get('edited') is True or value.get('edited') is not False and value.get('version', 1) > 1)


def _at(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class PromptStore:
    def __init__(self, root):
        self.root = Path(root)

    def document(self, name):
        parts = name.split('/') if isinstance(name, str) else ['']
        if not name or any(not p or p.startswith('.') for p in parts) or any(c in name for c in ('\\', '\0')):
            raise ValueError('Use the prompt name returned by list')
        return Path(str(self.root.joinpath(*parts)) + '.json')

    def _default(self, name):
        path = self.root / '.defaults' / self.document(name).relative_to(self.root)
        return json.loads(path.read_text()) if path.exists() else None

    def list(self):
        names = {json.loads(p.read_text())['name'] for p in self.root.rglob('*.json') if '.history' not in p.parts}
        return [self.read(name) for name in sorted(names) if not any(s.startswith('.') or not s for s in name.split('/'))]

    def read(self, name):
        target = self.document(name)
        value = json.loads(target.read_text()) if target.exists() else None
        default = self._default(name)
        if default and not edited(value):
            return {**{k: v for k, v in default.items() if k != 'previous_texts'}, 'version': (value or {}).get('version', 1), 'edited': False}
        if value is None:
            raise FileNotFoundError(target)
        if default and not assembled(name):
            changed, seen = _at(default.get('updated_at')), _at(value.get('default_seen_at') or value.get('updated_at'))
            if changed and seen and changed > seen:
                return {**value, 'stale': True, 'default_text': default['text']}
        return value

    @staticmethod
    def effective(name):
        if assembled(name):
            return '这段按每次调用的上下文拼出；保存后固定为保存的整段文本，按上下文变化的分段和之后的代码更新都不再进入。restore default 恢复跟随代码。'
        if name == 'persona':
            return '下一次 DS/Kimi 请求或原生回合读取。'
        if name == 'summary/session':
            return 'DS/Kimi 在下一次摘要请求读取；原生直连在重新加载会话时读取。'
        if name.startswith('tools/'):
            return 'DS/Kimi 在下一次请求读取；原生直连的工具说明来自工具服务本身，不读取这里。'
        if name == 'system' or name.startswith('mcp/'):
            return 'DS/Kimi 在下一次请求读取；原生直连在重新加载会话时读取。'
        return '使用这段提示词的下一次调用读取。'

    def history(self, name, version=None):
        """Earlier versions newest first, without their text; one version with its text when named."""
        self.document(name)
        folder = self.root / '.history' / name
        if version is not None:
            current = self.read(name)
            if int(version) == current.get('version'):
                return current
            path = folder / (str(int(version)) + '.json')
            if not path.exists():
                raise FileNotFoundError('No such prompt version')
            return json.loads(path.read_text())
        brief = lambda v: {**{k: x for k, x in v.items() if k not in ('text', 'default_text', 'previous_texts')}, 'chars': len(v.get('text', ''))}
        rows = [json.loads(p.read_text()) for p in folder.glob('*.json')] if folder.exists() else []
        return {'name': name, 'current': brief(self.read(name)),
                'versions': [brief(v) for v in sorted(rows, key=lambda v: v.get('version', 0), reverse=True)]}

    def write(self, name, text, **provenance):
        return self._save(name, text, provenance)

    def restore(self, name, version, **provenance):
        """Save an earlier version again as the newest; `default` follows the code default again."""
        if version == 'default':
            default = self._default(name)
            if not default:
                raise FileNotFoundError('This prompt has no code default')
            return self._save(name, default['text'], {**provenance, 'restored_from': 'default'}, follow=True)
        return self._save(name, self.history(name, version)['text'], {**provenance, 'restored_from': int(version)})

    def _save(self, name, text, provenance, follow=False):
        target = self.document(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(self.root / '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                prior = self.read(name)
            except FileNotFoundError:
                prior = None
            default = self._default(name)
            seen = (default or {}).get('updated_at')
            if prior and prior['text'] == text and (not follow or not edited(prior)):
                if not prior.get('stale'):
                    return {**prior, 'state': 'unchanged', 'path': str(target), 'effective': self.effective(name)}
                # Keeping an edit over a newer code default is a decision, recorded without a new version.
                value = {**{k: v for k, v in prior.items() if k not in ('stale', 'default_text')}, 'default_seen_at': seen}
                state = 'kept'
            else:
                if prior:
                    history = self.root / '.history' / name
                    history.mkdir(parents=True, exist_ok=True)
                    kept = {k: v for k, v in prior.items() if k not in ('stale', 'default_text')}
                    (history / (str(prior['version']) + '.json')).write_text(json.dumps(kept, ensure_ascii=False))
                value = {'name': name, 'version': (prior or {}).get('version', 0) + 1, 'text': text, 'edited': not follow,
                         'updated_at': datetime.now(timezone.utc).isoformat(), 'default_seen_at': seen,
                         'source_input_id': provenance.get('source_input_id'),
                         'external_read': provenance.get('external_read'),
                         **({'restored_from': provenance['restored_from']} if 'restored_from' in provenance else {}),
                         **({'migrated_from': provenance['migrated_from']} if 'migrated_from' in provenance else {})}
                state = 'saved'
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False))
            os.replace(temporary, target)
            return {**value, 'state': state, 'path': str(target), 'effective': self.effective(name)}


def carry_renamed(store):
    """Once, at start-up: an edit saved under a prompt's old name becomes a new version of its new
    name, while that one is absent or follows its default. A plain save recorded as a migration (no
    input, no notice); the old document stays. Answers the names it wrote."""
    carried = []
    for old, new in RENAMED.items():
        source = store.document(old)
        value = json.loads(source.read_text()) if source.exists() else None
        target = store.document(new)
        current = json.loads(target.read_text()) if target.exists() else None
        folder = store.root / '.history' / new
        earlier = [json.loads(p.read_text()) for p in folder.glob('*.json')] if folder.exists() else []
        if not edited(value) or edited(current) or any(v.get('migrated_from') == old for v in [current or {}, *earlier]):
            continue
        store.write(new, value['text'], migrated_from=old)
        carried.append(new)
    return carried


def project_prompt(name, fallback, root):
    """Register the current code text, then apply a saved KIN edit if present."""
    store = PromptStore(Path(root) / 'prompts')
    target = store.document(name)
    default = store.root / '.defaults' / target.relative_to(store.root)
    default.parent.mkdir(parents=True, exist_ok=True)
    prior = json.loads(default.read_text()) if default.exists() else None
    if not prior or prior['text'] != fallback:
        previous = [t for t in (prior or {}).get('previous_texts', []) if t != (prior or {}).get('text')]
        temporary = default.with_name(default.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({'name': name, 'version': 1, 'source': 'default', 'text': fallback,
                                        'previous_texts': [*previous, *([prior['text']] if prior else [])][-MAX_PREVIOUS_TEXTS:],
                                        'updated_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
        os.replace(temporary, default)
    return store.read(name)['text']


def project_schema(name, schema, root):
    """Schema prose is editable; fields and types continue to describe the actual API."""
    if isinstance(schema, list):
        return [project_schema(f"{name}/{i}", value, root) for i, value in enumerate(schema)]
    if not isinstance(schema, dict):
        return schema
    return {key: project_prompt(f"{name}/{key}", value, root)
            if key in {"description", "title"} and isinstance(value, str)
            else project_schema(f"{name}/{key}", value, root)
            for key, value in schema.items()}


def mcp_instructions(fallback, root):
    return '[KIN_PROJECT_PROMPT:mcp/instructions]\n' + project_prompt('mcp/instructions', fallback, root) + '\n[/KIN_PROJECT_PROMPT:mcp/instructions]'
