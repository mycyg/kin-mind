"""JSON CLI for trusted local host adapters; all content arrives on stdin."""

import argparse
import json
import sys
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.models import Scope

from .continuity import ConcernChange, ContinuityConfig
from .state import AffectiveEvent, DesireChange, Mind


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "action",
        choices=[
            "read",
            "initialize",
            "record",
            "desire",
            "candidate",
            "claim",
            "check",
            "settle",
            "reconsider",
            "configure-behavior",
            "configure-continuity",
            "concern",
        ],
    )
    args = parser.parse_args()
    request = json.load(sys.stdin)
    mind = Mind(Engine(args.root), Scope.model_validate(request.pop("scope")))
    if args.action == "read":
        result = mind.read(**request)
    elif args.action == "initialize":
        result = mind.initialize(**request)
    elif args.action == "record":
        result = mind.record(AffectiveEvent.model_validate(request))
    elif args.action == "desire":
        result = mind.manage_desire(DesireChange.model_validate(request))
    elif args.action == "concern":
        result = mind.manage_concern(ConcernChange.model_validate(request))
    elif args.action == "configure-continuity":
        result = mind.configure_continuity(ContinuityConfig.model_validate(request))
    elif args.action == "candidate":
        result = mind.contact_candidate()
    elif args.action == "claim":
        result = mind.claim_contact(**request)
    elif args.action == "check":
        result = mind.check_contact(**request)
    elif args.action == "reconsider":
        result = mind.reconsider_contacts(**request)
    elif args.action == "configure-behavior":
        result = mind.configure_behavior(request)
    else:
        result = mind.settle_contact(**request)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
