"""The schema a fork's structured final is held to, and its answer read back.

`_kin/assess` hands its schema to the model API as a strict structured output: every property
in `required`, `additionalProperties` false on every object, no maps, and only a core of
keywords. A pydantic schema says more than that core -- optional fields, dict fields, string and
number bounds -- and the API refuses the whole request (`invalid_json_schema`): after WS4 moved
the main-session assessment into a fork, not one fork assessment could run (2026-09-27).
`strict_schema` states the same shape inside that core:

- every property is required; one the model may leave out also takes null, and `decode` drops
  that null again, so the model's default applies as if it had been left out;
- a dict becomes a list of {key, value} entries, and a value of any shape its JSON text;
- the bounds strict mode cannot carry (lengths, ranges, counts, patterns, formats) become words
  in the field's description; the validation after the answer still holds every one of them;
- `title`, `default` and `examples` are left out, and a `$ref` stands alone.

`decode` reads an answer back into the shape the pydantic model reads. It walks the original
schema, so an answer already in that shape (the legacy channel pastes the schema into its
prompt) passes through unchanged.
"""
import json

# The conservative core of strict structured outputs (Azure OpenAI's documented list of what it
# does not take, 2026-08, is the narrower of the two published lists). Anything else is left out.
STRICT_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items",
                             "enum", "const", "anyOf", "$ref", "$defs", "description"})
# The bounds strict mode cannot carry, in words.
_BOUNDS = (("minLength", "至少 {} 个字符"), ("maxLength", "至多 {} 个字符"),
           ("minimum", "不小于 {}"), ("exclusiveMinimum", "大于 {}"),
           ("maximum", "不大于 {}"), ("exclusiveMaximum", "小于 {}"), ("multipleOf", "为 {} 的倍数"),
           ("minItems", "至少 {} 项"), ("maxItems", "至多 {} 项"),
           ("minProperties", "至少 {} 项"), ("maxProperties", "至多 {} 项"),
           ("pattern", "符合正则 {}"), ("format", "格式为 {}"))
_ENTRIES = "键值对列表：每项的 key 是键，value 是值，键不重复"
_JSON_TEXT = "一个 JSON 值的文本"
_PRIMITIVES = frozenset({"string", "number", "integer", "boolean"})


def strict_schema(schema):
    """`schema`, a pydantic JSON schema with its `$defs` at the root, as a strict one. Strict mode
    takes only an object at the root: any other schema is sent as it is, for the API to judge."""
    if _shape(schema) != "object":
        return schema
    body = _encode({key: value for key, value in schema.items() if key != "$defs"})
    definitions = schema.get("$defs") or {}
    if definitions:
        body["$defs"] = {name: _encode(node) for name, node in definitions.items()}
    return body


def decode(schema, value):
    """An answer to `strict_schema(schema)`, in the shape `schema` describes."""
    return _decode(schema, value, schema.get("$defs") or {})


def _words(node, extra=()):
    notes = [text.format(json.dumps(node[key], ensure_ascii=False) if isinstance(node[key], str) else node[key])
             for key, text in _BOUNDS if key in node]
    if node.get("uniqueItems"):
        notes.append("各项不重复")
    notes.extend(extra)
    description = node.get("description") if isinstance(node.get("description"), str) else ""
    if notes:
        description = (description + " " if description else "") + "（" + "；".join(notes) + "）"
    return {"description": description} if description else {}


def _shape(node):
    """What a node of the original schema holds: object, map (a dict, its values typed when
    `additionalProperties` is a schema), any, array or scalar."""
    if not isinstance(node, dict):
        return "any"
    kind, properties, extra = node.get("type"), node.get("properties"), node.get("additionalProperties")
    if kind == "object" or properties is not None or extra is not None:
        return "object" if properties or extra is False else "map"
    if kind == "array":
        return "array"
    if kind is None and not any(key in node for key in ("enum", "const", "anyOf", "oneOf", "allOf", "$ref")):
        return "any"
    return "scalar"


def _takes_null(node):
    kind = node.get("type")
    return (kind == "null" or (isinstance(kind, list) and "null" in kind)
            or any(isinstance(branch, dict) and _takes_null(branch) for branch in node.get("anyOf", ()))
            or None in node.get("enum", ()) or ("const" in node and node["const"] is None))


def _with_null(node):
    if _takes_null(node):
        return node
    if "anyOf" in node:
        return {**node, "anyOf": [*node["anyOf"], {"type": "null"}]}
    kind = node.get("type")
    if isinstance(kind, str) and kind in _PRIMITIVES and "enum" not in node and "const" not in node:
        return {**node, "type": [kind, "null"]}
    return {"anyOf": [node, {"type": "null"}]}


def _map_values(node):
    """The schema of a dict's values: `additionalProperties` when it is one, anything otherwise."""
    extra = node.get("additionalProperties")
    return extra if isinstance(extra, dict) else {}


def _entry(value):
    return {"type": "object", "properties": {"key": {"type": "string"}, "value": value},
            "required": ["key", "value"], "additionalProperties": False}


def _encode(node):
    if not isinstance(node, dict):
        # `true` (anything) and `false` do not occur as property schemas in pydantic's output;
        # anything reads as its JSON text.
        return {"type": "string", "description": _JSON_TEXT}
    if "$ref" in node:
        return {"$ref": node["$ref"]}
    if len(node.get("allOf") or ()) == 1:
        # An older pydantic wraps a described reference: the reference, in the words around it.
        inner = node["allOf"][0]
        return _encode(inner) if "$ref" in inner else _encode({**{k: v for k, v in node.items() if k != "allOf"}, **inner})
    branches = node.get("anyOf") or node.get("oneOf")
    if branches:
        return {"anyOf": [_encode(branch) for branch in branches], **_words(node)}
    shape = _shape(node)
    if shape == "object":
        required = set(node.get("required") or ())
        properties = node.get("properties") or {}
        encoded = {name: _encode(child) for name, child in properties.items()}
        return {"type": "object",
                "properties": {name: child if name in required else _with_null(child) for name, child in encoded.items()},
                "required": list(properties), "additionalProperties": False, **_words(node)}
    if shape == "map":
        return {"type": "array", "items": _entry(_encode(_map_values(node))), **_words(node, (_ENTRIES,))}
    if shape == "any":
        return {"type": "string", **_words(node, (_JSON_TEXT,))}
    if shape == "array":
        return {"type": "array", "items": _encode(node.get("items") or {}), **_words(node)}
    return {**{key: node[key] for key in ("type", "enum", "const") if key in node}, **_words(node)}


def _resolve(node, definitions):
    for _ in range(64):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            node = definitions.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
        elif len(node.get("allOf") or ()) == 1:
            node = {**{k: v for k, v in node.items() if k != "allOf"}, **node["allOf"][0]}
        else:
            return node
    return node


def _accepts_null(node, definitions):
    node = _resolve(node, definitions)
    if not isinstance(node, dict):
        return True
    return _takes_null(node) or any(_accepts_null(branch, definitions) for branch in node.get("anyOf") or node.get("oneOf") or ())


def _fits(node, value, definitions):
    node = _resolve(node, definitions)
    if value is None:
        return _accepts_null(node, definitions)
    shape = _shape(node)
    if shape == "object":
        return isinstance(value, dict)
    if shape == "map":
        return isinstance(value, (list, dict))
    if shape == "array":
        return isinstance(value, list)
    if shape == "any":
        return True
    kinds = node.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
    if not kinds:
        return value in node.get("enum", [value]) and node.get("const", value) == value
    checks = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    return any(isinstance(value, checks.get(kind, object)) and not (kind in {"integer", "number"} and isinstance(value, bool))
               for kind in kinds)


def _decode(node, value, definitions):
    node = _resolve(node, definitions)
    if value is None or not isinstance(node, dict):
        return value
    branches = node.get("anyOf") or node.get("oneOf")
    if branches:
        for branch in branches:
            if _fits(branch, value, definitions):
                return _decode(branch, value, definitions)
        return value
    shape = _shape(node)
    if shape == "any":
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    if shape == "map":
        values = _map_values(node)
        if isinstance(value, list) and all(isinstance(entry, dict) and set(entry) == {"key", "value"} for entry in value):
            decoded = {}
            for entry in value:
                key = entry["key"]
                if not isinstance(key, str) or key in decoded:
                    raise ValueError("strict-map-key-invalid-or-duplicate")
                decoded[key] = _decode(values, entry["value"], definitions)
            return decoded
        # A dict answered as a dict (the original shape): its typed values are read on; values of any
        # shape stay exactly as given, a string never taken for JSON text.
        if isinstance(value, dict) and values:
            return {key: _decode(values, item, definitions) for key, item in value.items()}
        return value
    if shape == "object" and isinstance(value, dict):
        properties, required = node.get("properties") or {}, set(node.get("required") or ())
        decoded = {}
        for key, item in value.items():
            if key not in properties:
                decoded[key] = item
            elif item is None and key not in required and not _accepts_null(properties[key], definitions):
                continue
            else:
                decoded[key] = _decode(properties[key], item, definitions)
        return decoded
    if shape == "array" and isinstance(value, list):
        return [_decode(node.get("items") or {}, item, definitions) for item in value]
    return value
