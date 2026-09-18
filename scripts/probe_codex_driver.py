#!/usr/bin/env python3
"""Probe driver: run one real codex exploration attempt and print the receipt.

Usage: probe_codex_driver.py <request.json>

The request JSON names everything explicitly — executable, workdir, model,
reasoning, provider (id/base_url/env_key/wire_api), topic payload, optional
budget_seconds and computer config. Nothing is read from ~/.codex or any
deployment config; the credential value arrives through the named environment
variable only. The receipt is printed to stdout as JSON; nothing is written
outside the workdir.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kin_mind.codex_executor import run_codex


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    receipt = run_codex(
        request["executable"],
        request["topic"],
        request["workdir"],
        budget_seconds=request.get("budget_seconds", 1200),
        model=request["model"],
        reasoning=request["reasoning"],
        provider=request["provider"],
        computer=request.get("computer"),
    )
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
