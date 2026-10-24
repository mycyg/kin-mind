"""Versioned project prompt text. A save replaces one document atomically."""
from pathlib import Path
import json
import os
import uuid
import fcntl
from datetime import datetime, timezone

class PromptStore:
    def __init__(self, root):
        self.root = Path(root)

    def document(self, name):
        parts = name.split('/')
        if not name or any(p in ('', '.', '..') for p in parts) or any(c in name for c in ('\\', '\0')):
            raise ValueError('Use the prompt name returned by list')
        return Path(str(self.root.joinpath(*parts)) + '.json')

    def list(self):
        names = {json.loads(p.read_text())['name'] for p in self.root.rglob('*.json') if '.history' not in p.parts}
        return [self.read(name) for name in sorted(names)]

    def read(self, name):
        target = self.document(name)
        value = json.loads(target.read_text()) if target.exists() else None
        default = self.root / '.defaults' / target.relative_to(self.root)
        if (not value or not value.get('edited') and value.get('version', 1) == 1) and default.exists():
            return json.loads(default.read_text())
        if value is None:
            raise FileNotFoundError(target)
        return value

    @staticmethod
    def effective(name):
        if name == 'persona':
            return '下一次 DS/Kimi 请求或原生回合读取。'
        if name == 'system' or name.startswith(('mcp/', 'tools/')):
            return 'DS/Kimi 在下一次请求读取；原生直连在重新加载会话时读取。'
        return '使用这段提示词的下一次调用读取。'

    def write(self, name, text):
        target = self.document(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(self.root / '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                prior = self.read(name)
            except FileNotFoundError:
                prior = None
            if prior and prior['text'] == text:
                return {**prior, 'state':'unchanged', 'path':str(target), 'effective': self.effective(name)}
            if prior:
                history = self.root / '.history' / name
                history.mkdir(parents=True, exist_ok=True)
                (history / (str(prior['version']) + '.json')).write_text(json.dumps(prior, ensure_ascii=False))
            value = {'name': name, 'version': (prior or {}).get('version', 0) + 1, 'text': text, 'edited': True,
                     'updated_at': datetime.now(timezone.utc).isoformat()}
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False))
            os.replace(temporary, target)
            return {**value, 'state':'saved', 'path': str(target), 'effective': self.effective(name)}


def project_prompt(name, fallback, root):
    """Register the current code text, then apply a saved KIN edit if present."""
    store = PromptStore(Path(root) / 'prompts')
    target = store.document(name)
    default = store.root / '.defaults' / target.relative_to(store.root)
    default.parent.mkdir(parents=True, exist_ok=True)
    prior = json.loads(default.read_text()) if default.exists() else None
    if not prior or prior['text'] != fallback:
        temporary = default.with_name(default.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({'name': name, 'version': 1, 'source': 'default', 'text': fallback,
                                        'previous_texts': list(dict.fromkeys([*((prior or {}).get('previous_texts', [])), *([prior['text']] if prior else [])])),
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
