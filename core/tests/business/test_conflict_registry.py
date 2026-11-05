"""Every static conflict message on the appraisal commit path is registered (K4-07): one that
is not is counted as `unknown`, and the attempt ledger and the quarantine reasons lose what it
was. A message changed at its raise site must be changed here too."""
import ast
from pathlib import Path

import kin_mind
from kin_mind.conflicts import COMMIT_PATH_MODULES, REGISTRY, classify
from eventmem.core.db import Conflict

SOURCE = Path(kin_mind.__file__).resolve().parents[1]


def literal_raises(module):
    tree = ast.parse((SOURCE / (module.replace(".", "/") + ".py")).read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        first = node.exc.args[0] if node.exc.args else None
        if name in {"Conflict", "Missing"} and isinstance(first, ast.Constant) and isinstance(first.value, str):
            if not {"code", "kind"} & {keyword.arg for keyword in node.exc.keywords}:
                yield node.lineno, first.value


def test_every_commit_path_message_is_registered():
    unregistered = [(module, line, text) for module in COMMIT_PATH_MODULES
                    for line, text in literal_raises(module) if text not in REGISTRY]
    assert unregistered == []


def test_a_registered_message_is_classified_by_its_entry():
    def replay():
        raise Conflict("Procedure needs review before replay")

    try:
        replay()
    except Conflict as error:  # the registry reads the literal at the raise site
        found = classify(error)
    assert (found.kind, found.code, found.handling) == ("runtime", "procedure-needs-review", "block")
