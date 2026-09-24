"""Check the wheel this build produced (T1-14).

    python scripts/check_wheel.py [wheel]

Without an argument it takes the one dist/kin_mind-<pyproject version>-*.whl and refuses when
there is none or more than one. The wheel must carry this source tree's modules byte for byte, so
an older build left under the same version cannot pass for this one."""

import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None, root: Path = ROOT):
    argv = sys.argv[1:] if argv is None else argv
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    wheels = [Path(a) for a in argv] or sorted((root / "dist").glob(f"kin_mind-{version}-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one kin_mind-{version} wheel, found {len(wheels)}; "
                         "clear dist/ of other builds or name the wheel")
    with ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        for required in [
            "eventmem/web/index.html",
            "eventmem/core/engine.py",
            "kin_mind/state.py",
            "kin_mind/profile.py",
            "eventmem/sdk/__init__.pyi",
            "eventmem/sdk/py.typed",
        ]:
            assert required in names, required
        assert any(n.startswith("eventmem/web/assets/") and n.endswith(".js") for n in names)
        assert not any("/.env" in n or "/memory.sqlite3" in n for n in names)
        source = {path.relative_to(root / "src").as_posix(): path
                  for package in ("eventmem", "kin_mind")
                  for path in (root / "src" / package).rglob("*.py") if "__pycache__" not in path.parts}
        missing = sorted(set(source) - names)
        stale = sorted(n for n in set(source) & names if archive.read(n) != source[n].read_bytes())
        extra = sorted(n for n in names if n.endswith(".py") and n.split("/")[0] in ("eventmem", "kin_mind")
                       and n not in source)
        assert not (missing or stale or extra), (
            f"{wheels[0].name} is not this tree: missing {missing[:5]}, stale {stale[:5]}, extra {extra[:5]}")
    print(f"{wheels[0].name} carries this tree's core, typed SDK and console assets")


if __name__ == "__main__":
    main()
