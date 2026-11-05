import json
from pathlib import Path

from scripts.compact_prompt_history import _scan


def test_plan_names_only_history_beyond_the_cap_and_retires_the_unnamed_tool(tmp_path):
    root = tmp_path / "prompts"
    (root / ".defaults").mkdir(parents=True)
    (root / ".defaults/persona.json").write_text(json.dumps({
        "name": "persona", "version": 1, "text": "current",
        "previous_texts": ["a", "b", "c", "d", "e"],
    }))
    (root / "tools").mkdir(parents=True)
    (root / "tools/undefined.json").write_text(json.dumps({"name": "tools/undefined", "version": 1, "text": "old"}))
    (root / ".defaults/tools/read_memory/schema/properties/query").mkdir(parents=True)
    (root / ".defaults/tools/read_memory/schema/properties/query/title.json").write_text("{}")
    plan = _scan(root)
    assert [str(relative) for relative, _, _ in plan["trims"]] == [".defaults/persona.json"]
    assert plan["trims"][0][1] == ["d", "e"]
    assert plan["schema"] == [".defaults/tools/read_memory/schema/properties/query/title.json"]
    assert plan["retired"] == ["tools/undefined.json"]
