# dsh-eventmem 1.0

MemoryPalace 的 DeepSeek Harness 原生 Cordis 插件。记忆采集、召回、上下文预算、会话恢复与后台整理都由本地 Python 服务完成，插件和其他宿主共用这一套核心逻辑。

## 安装

先按 [MemoryPalace 指南](../MEMORYPALACE.md)的“安装与启动”构建并启动服务，再构建插件：

```sh
cd dsh-plugin
npm ci
npm run build
```

在 Harness 的 `cordis.patch.yml` 中通过本地绝对路径加载：

```yaml
- insert:
    - id: eventmem
      name: '/absolute/path/to/memory-palace/dsh-plugin/lib/index.js'
      config:
        enabled: true
```

配置项都有默认值：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `enabled` | `true` | 总开关；关闭后所有监听器立即返回 |
| `memoryDirName` | `.memory` | 项目内护栏日志的目录名：监听器出错时写入 `<项目>/<memoryDirName>/log/eventmem-dsh.log` |
| `injectWorkingSet` | `true` | 会话启动与压缩之后注入服务返回的工作集 |
| `writeFeed` | `true` | 把消息、工具结果、todo 快照与 turn／step 边界发给服务 |

启用的插件如果配置里仍写着 `legacyMode: true`，加载时直接报错，不会改用服务传输。

使用 `dsh --dump-config` 检查合并后的配置。验证所用的 Harness peer 版本为 `0.1.1-rc.2`；依赖锁保留实际版本，升级预发布版后应重新执行兼容测试。

## 行为

- `agent/session-start`：通知服务会话开始；`source: compact` 使用压缩恢复边界。
- `session/event`：记录用户与助手消息、todo 和 turn／step 事件；插件注入内容不被记录为用户消息。
- `tools/execute`：操作前向服务查询相关经验并注入，查询结束（单次请求 7 秒超时）后才执行工具；`tools/result`：记录行动与结果。
- `session/flush`：等待该会话的传输队列。会话释放与插件卸载时发送结束事件。
- 每个事件先写入私有 spool（`EVENTMEM_HOME` 下的 `host-spool`），服务接收后才删除；服务不可用时由服务的后台循环稍后重放，会话启动与操作前查询不重放。插件不自行计算另一套召回结果。

`EVENTMEM_HOME` 默认 `~/.memorypalace`，`EVENTMEM_URL` 默认 `http://127.0.0.1:8319`。服务与宿主使用相同根目录和凭据。上下文注入遵循 Harness 的 `agent.inject` 语义，可能进入后续模型请求；它不保证撤销已经发出的工具调用。

## 验证

```sh
npm run typecheck
npm test
npm run build
```

`npm test` 先构建，再运行根目录的 `tests/clients/plugin.mjs`：覆盖服务传输下的事件顺序（启动、消息、操作前查询、压缩、结束）、上下文注入、断连时事件留在 spool 中，以及加载时拒绝 `legacyMode: true` 的配置。HTTP、Python／TypeScript SDK 与 CLI 的一致性测试位于根目录 `tests/business/test_protocols_v1.py`。
