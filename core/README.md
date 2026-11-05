# Kin Core

Python 内核，负责带来源的记忆、情绪、评估、日记、梦境和自主计划。桌面与渠道由同仓库的 `dsh-kin` 插件提供，完整安装和产品说明见 [项目 README](../README.md)。

```bash
uv sync --python 3.13 --frozen --extra dev
TIKTOKEN_CACHE_DIR=../.dev/tiktoken uv run python scripts/prepare_test_tokenizer.py
TIKTOKEN_CACHE_DIR=../.dev/tiktoken uv run pytest -q
```

可按需添加 `vector`、`graph` 或 `media`。`media` 与 `local-embedding` 的依赖冲突，需分别安装到独立环境，不要使用 `--all-extras`。

运维细节见 [operations.md](docs/operations.md)。真实数据和凭据保存在所配置的数据目录中，仓库只提供代码与合成测试。
