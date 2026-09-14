"""生成黄金 fixture：把 Python 侧函数的输出存成 JSON，供 TS 测试逐字节对照。

两套实现的互操作生命线是三处规约必须逐字节一致：
  1. recall.error_signature —— 错误倒排的 key
  2. index.tokenize / index.intent_tokens —— intent 倒排的 key
  3. paths.MemoryPaths.relative —— 文件倒排的 key

用法（在 event-memory/ 下）：

    ./.venv/bin/python dsh-plugin/scripts/gen-fixtures.py

fixture 与 Python 源码同步更新；改动 Python 侧上述三处后必须重跑本脚本，
TS 测试会立刻暴露漂移。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from eventmem.index import intent_tokens, tokenize  # noqa: E402
from eventmem.paths import MemoryPaths  # noqa: E402
from eventmem.recall import error_signature  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# 20 个代表性错误输入：中文、traceback、POSIX/Windows 路径、十六进制、时间戳、
# 空白与空输入、已是签名的幂等输入、非 ASCII 数字、超长行。
SIGNATURE_INPUTS: list[str] = [
    "",
    "   \n\t  \n",
    "ValueError: port busy",
    (
        "Traceback (most recent call last):\n"
        '  File "/Users/synthetic/proj/train/launcher.py", line 10, in <module>\n'
        "    raise ValueError('port busy')\n"
        "ValueError: port busy"
    ),
    (
        "Traceback (most recent call last):\n"
        '  File "/opt/app/服务/主程序.py", line 42, in run\n'
        "RuntimeError: 端口 8080 已被占用"
    ),
    "2026-08-25T14:32:01.123Z ERROR: connection refused at /var/run/app.sock",
    "2026-08-25 14:32:01+08:00 fatal: unable to access '/srv/git/repo.git'",
    "14:32:01.999 WARN pool exhausted",
    "Segmentation fault at address 0x7fff5fbff8c0 in module libfoo.so",
    "malloc(): corrupted top size 0X1F3A near 0xdeadBEEF",
    "  File \"C:\\Users\\apple\\proj\\train\\launcher.py\", line 10, in <module>",
    "error: cannot open C:\\Program\\data\\db.sqlite3\\ for writing",
    "中文错误：/usr/local/lib/python3.13/site-packages/torch/nn.py 加载失败",
    "模块加载失败，路径为 /opt/模型/权重.safetensors，错误码 0x8007000E",
    "webpack.config.js:120:15 - Module not found: Can't resolve './missing'",
    "src/eventmem/recall.py:97 IndexError: list index out of range",
    "AssertionError: expected 3 got 4 (see line 88 and LINE 12)",
    "ValueError: port busy",  # 幂等：已是签名的入参不应被二次改写
    "E   assert  0.030   ==   0.045",
    "ERROR at ٢٠٢٦-٠٨-٢٥T١٤:٣٢:٠١ non-ascii digits",
    "第一行有内容\n第二行也有内容",
    "OSError: [Errno 24] Too many open files: '/tmp/x/y/z/verylongname_" + "a" * 200 + ".txt'",
]

# 词元化输入：拉丁、中文、日文假名、CJK 扩展 A、混排、大小写、标点、单字符、空。
TOKEN_INPUTS: list[str] = [
    "",
    "   ",
    "fix Ray port conflict",
    "Fix RAY Port CONFLICT",
    "修复 Ray 端口冲突",
    "端口",
    "口",
    "重建索引与工作集",
    "ポート競合を修正する",
    "㐀㐁㐂 扩展区",
    "train/launcher.py 里的 8080 端口",
    "a1b2c3 42 x",
    "——、。！？",
    "混合 mixed 文本 text 一二三",
    "GPU·小时 79",
    "checkpoint 存档 rollback 回滚",
    "ß ẞ İ i̇ ſ",
    "emoji 🙂 不参与分词",
]

FIXTURE_PROJECT = "/tmp/eventmem-fixtures/project"

# 文件 key 规约输入：项目内绝对路径（存在/不存在）、项目外、相对、重复斜杠、
# `.` 与 `..` 段、中文路径、结尾斜杠。
FILE_INPUTS: list[str] = [
    f"{FIXTURE_PROJECT}/src/foo.py",
    f"{FIXTURE_PROJECT}/src/missing/deep/bar.py",
    f"{FIXTURE_PROJECT}/src//foo.py",
    f"{FIXTURE_PROJECT}/./src/foo.py",
    f"{FIXTURE_PROJECT}/src/../src/foo.py",
    f"{FIXTURE_PROJECT}/中文目录/文件.md",
    f"{FIXTURE_PROJECT}/",
    FIXTURE_PROJECT,
    "/etc/hosts",
    "/tmp/eventmem-fixtures/other/baz.py",
    "src/foo.py",
    "./src/foo.py",
    "src//foo.py",
    "../outside/foo.py",
    "foo.py",
    "",
    ".",
    "//double/leading",
    "///triple/leading",
    "src/foo.py/",
]


def build_project() -> Path:
    """建出 fixture 用的项目目录树，让 `Path.resolve()` 有真实的符号链接可解。"""
    root = Path(FIXTURE_PROJECT)
    for sub in ("src", "中文目录"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "src" / "foo.py").write_text("# fixture\n", encoding="utf-8")
    (root / "中文目录" / "文件.md").write_text("# fixture\n", encoding="utf-8")
    (Path("/tmp/eventmem-fixtures") / "other").mkdir(parents=True, exist_ok=True)
    (Path("/tmp/eventmem-fixtures") / "other" / "baz.py").write_text("# fixture\n", encoding="utf-8")
    return root


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    build_project()
    paths = MemoryPaths.for_project(Path(FIXTURE_PROJECT))

    signatures = [{"input": text, "output": error_signature(text)} for text in SIGNATURE_INPUTS]
    tokens = [
        {"input": text, "tokens": tokenize(text), "intentTokens": intent_tokens(text)}
        for text in TOKEN_INPUTS
    ]
    file_keys = {
        "projectDir": FIXTURE_PROJECT,
        "resolvedProjectDir": str(paths.project_dir),
        "cases": [{"input": text, "output": paths.relative(text)} for text in FILE_INPUTS],
    }

    for name, payload in (
        ("signatures.json", signatures),
        ("tokens.json", tokens),
        ("file-keys.json", file_keys),
    ):
        (OUT / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写出 {OUT / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
