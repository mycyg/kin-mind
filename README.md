# KIN (`dsh-kin`)

KIN 是常驻在电脑里的陪伴者，也是你在 DeepSeek Harness（DSH）里的团队大总管。

她以 **`dsh-kin` 插件包（Bundle）** 运行在 DSH 之上，带着连续的记忆、随经历变化的心境、日记、梦境、心愿和长期计划。你把事情交给她，她能调度帮手去做，再把结果带回原来的对话。

---

## 界面一览

> 以下截图均为真实产品界面（搭配离线合成演示数据拍摄）。

### 此刻（Now）

左侧是 KIN 的专属对话面板（流式回复、正在思考与调用帮手的轻提示、历史翻阅）；右侧展示她当下的主导感受、各项情绪与驱动力维度、刚想到的心事，以及下次自主回顾的时间。

![KIN 此刻界面（真实产品界面，使用合成演示数据）](docs/images/kin-home.jpg)

### 她的小世界（Her World）

以液态玻璃卡片收纳她的内在生活：**梦境（Dreams）**、**书信 / 时光信（Letters）**、**可回复的日记（Diary）**、**心愿与计划（Wishes）**、**出门探索（Wander）** 与 **回忆检索与更正（Memories）**。前台保留完整的液态玻璃光泽与随心境漂移的天空背景；当窗口隐藏或失焦时，背景动画会自动暂停以节省资源。

![KIN 小世界界面（真实产品界面，使用合成演示数据）](docs/images/kin-world.jpg)

### 设置（Settings）

在页面内直接管理她的人设、聊天线与做事线模型路由、内心生活开关、认识的人与主动联系规则（如安静时段）、微信 / 飞书渠道账号与语音配置，以及健康自检与冻结开关。

![KIN 设置界面（真实产品界面，使用合成演示数据）](docs/images/kin-settings.jpg)

---

## 核心特性

- **常驻心智与内在生活**
  - **连续记忆与视角感知**：每句话按说话人、收件人与所属会话入库，回忆自带本地时间戳与人物称呼；支持在界面上对记错的内容直接批注更正（`correct_memory`）。亲密主动面向主人；她自己的对话笔记保留所属对话视角。
  - **真实心境注入**：每轮对话前将她此刻的感受、挂念的事与上次评估定下的在场态度作为事实交给模型，由模型按人设自然拿捏语气，不把数值硬编码成固定套话。
  - **按节奏触发的内心活动**：宿主心跳每分钟检查一次是否有到期工作（对话进行中自动让路，聊完再看）。评估、写日记、做梦、冒出心愿与出门探索均按她的内在状态、节律与评估决定触发，而非每分钟机械生成。
  - **可执行的计划与心愿**：计划不仅停留在想法上，还能推进、暂停与完结。常规评估与当前列表只加载进行中（`active` / `paused`）的计划，已完成或取消的计划自动退出活跃上下文，不再挤占评估预算，但仍可随时按编号或状态在历史中查阅。
  - **人设即改即生效**：无论在设置页编辑还是在对话中通过 `kin_persona` 修改，新的人设无需额外审批确认，立即写入 `persona.md` 并同步至 Core 的 `persona-policy.json`，从下一句回复起直接生效。

- **团队大总管与跨会话协同**
  - **独立可见的工单会话（`kin_delegate` / `kin_work`）**：当你把实际工作交给她时，她会在你指定的文件夹下开启标准的 DSH 顶层工作会话（使用 `standard` 预设与做事线模型）。你可以随时在 DSH 侧栏点开该会话查看实时进度、中途插话或叫停；帮手做完后，结果会回流到你和她的原对话中，由她用自己的话向你汇报并记入经历。
  - **轻量顺手帮手（`subagent`）**：对话中顺手查资料或做小事时，她会直接派生基于 `standard` 预设与做事线的子代理，在同一轮回复内拿回结果。帮手的行动边界受设置页“帮手的权限”（问我 / 自动审核 / 全权）统一约束。
  - **跨会话查阅与带话（`kin_conversations` / `kin_tell`）**：她能查看不同会话的上下文，也能帮你在桌面、微信、飞书或朋友与群聊之间转告消息，投递账本保留原 ID；未确认时先核对、限次补发，已送达的分段不会重复发送。

- **双模型线与多路自动回退**
  - **聊天线（`kin/chat`）**：负责她与你的直接对话、主动联系起草，优先保证语气与情感表达（默认 `low` 推理深度以降低首字延迟与 token 消耗，可按路线单独调整）。
  - **做事线（`kin/task`）**：负责后台心智评估、日记与梦境生成、记忆整理、出门探索以及所有工作帮手。
  - 每条线均可将 DSH 中已配置的多个模型排成有序列表；当上游尚未产生正文或完整工具调用就出错、首个有效流事件等待超过 60 秒，或返回空流时，会自动切换到下一条路线，API 密钥全程只保存在 DSH 内。

- **多端同心（桌面 · 微信 · 飞书 · 语音 · 表情包）**
  - 桌面端、微信与飞书共享同一个心智与记忆库。你在微信上随口提过的行程，回到桌面端问起时她依然记得，主动找你时也会发往你最近活跃的渠道。
  - 内置无外部重型 SDK 的微信（扫码登录、长轮询、CDN 媒体加解密）与飞书（长连接推送 + 定时补拉）传输层，支持 20 秒内连发消息合并、语音消息转写（ASR）、用她的声音回复语音条（TTS）以及发送表情包与点名文件。

---

## 架构概览：Core 与 DeepSeek Harness 怎么分工

KIN 采用 **“Python 心智内核（Core） + DeepSeek Harness 原生底座（`dsh-kin`）”** 的双层组合：

- **Python 心智（`core/`）负责“她是谁与记得什么”**：管理 SQLite / 向量记忆库、情绪与驱动力维度、内在评估（Appraisal）、日记与梦境、时光信、自主心愿与计划，以及 `eventmem` 后台记忆维护。
- **DeepSeek Harness（DSH）负责“怎么和世界交互与干活”**：提供会话控制器、跨供应商 LLM 适配、工具与权限体系、MCP 客户端、Skills、子代理与工作区管理，以及 Web / 桌面端 UI 渲染槽位。

```
DeepSeek Harness (>= 0.2.2-rc.1)
├─ 普通编码会话（standard 等预设）……………… 完全不受影响
└─ dsh-kin 插件包（cordis.patch.yml）
   ├─ 宿主插件 `dsh-kin`（kin/src/index.ts）
   │  ├─ Mind 进程管理 …………… 常驻 serve（快速读写）+ 限时模型动作进程 + 常驻 eventmem 工作者
   │  ├─ 虚拟模型路由 ……………… 注册 `kin/chat` 与 `kin/task` 两条有序回退线（models.ts）
   │  ├─ 本机回环 Bridge ………… 向 Core 提供本地 Messages 接口（走做事线）与 `/v1/kin/helper` 探索接口
   │  ├─ Heartbeat 分钟钟 ……… 每分钟检查 `review-due`、驱动评估 / 探索 / 创作 / 主动联系（对话时让路，冻结时暂停）
   │  └─ 功能模块 ……………………… talk（27 项 Core 工具与私房话/延后回复）、steward（大总管派活）、
   │                          contact（认人、连发合并、投递账本）、channels（微信/飞书/语音/表情）、health（看护/自检/冻结）
   ├─ 会话预设 `preset-kin`（仅作用于 KIN 会话）
   │  ├─ `@deepseek-ai/dsh-persona` + `{{kin_persona}}`（含人设与认识的人名册）
   │  ├─ `dsh-kin/preset`（每轮 `agent/pre-step` 注入回忆与心境快照、`session/event` 对话入库、压缩通知）
   │  └─ `@deepseek-ai/dsh-tool-web`、`dsh-tool-schedule`、`dsh-tool-subagent`（指向 `standard` + `kin/task`）
   └─ 前端界面 `dsh-kin/client`
      └─ 侧栏 KIN 入口 + 专属流式聊天框 + 液态玻璃 Dock（此刻 / 小世界 / 设置）
```

更详细的设计与模块拆解见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 环境要求与构建安装

### 1. 环境要求

- **DeepSeek Harness (DSH)**：`>= 0.2.2-rc.1 < 0.3.0`
- **Node.js**：`>= 24.9.0`
- **Python**：`3.13`（搭配 `uv` 管理虚拟环境）

### 2. 安装 Python 心智依赖（`core/`）

```bash
cd core
uv sync --python 3.13 --frozen --extra dev
```

> **注意**：请按需安装具体的 optional extra，**不要使用 `--all-extras`**（`media` 与 `local-embedding` 两个可选扩展存在依赖冲突）。

### 3. 安装并构建插件（`kin/`）

```bash
cd ../kin
npm ci
npm run build
```

构建完成后会生成 `kin/lib/`（含服务端入口 `lib/index.js`、预设 `lib/preset.js` 及前端包 `lib/client.js`）。

### 4. 装入 DSH 并启用

使用 DSH CLI 将本地 `kin` 目录添加到指定的 DSH profile（该命令会转发给对应 profile 的 `pnpm`）：

```bash
dsh plugin --profile <PROFILE_NAME> add <PATH_TO_REPO>/kin
```

`kin/package.json` 已声明 DSH Bundle 补丁入口：

```json
"dsh": {
  "bundle": {
    "patch": "./cordis.patch.yml"
  },
  "client": {
    "platform": "web"
  }
}
```

在对应 profile 的 `package.json` 中，把 `dsh-kin` 加入现有的 `dsh.profile.bundles` 数组，保留原来的 bundle，然后重启该 profile。启用该插件包后，[cordis.patch.yml](kin/cordis.patch.yml) 会自动挂载常驻宿主插件 `dsh-kin` 与会话预设 `preset-kin`。如需自定义路径，可在 `dsh-kin` 插件配置中指定：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `dataDir` | `~/.kin` | KIN 的独立心智数据目录（记忆库、人设 `persona.md`、会话表、模型路线与渠道状态） |
| `python` | `<repo>/core/.venv/bin/python` | 已安装 `kin-mind` 的 Python 3.13 解释器路径 |
| `coreSource` | `<repo>/core/src` | Core 源码目录路径 |
| `tokenizerCache` | 可选 | `tiktoken` 的 `cl100k_base` 本地缓存目录；不填则按字节估算 token |

### 5. 页面配置与旧数据迁移

1. **配置模型与渠道**：启动 DSH 后点击左侧栏的 **KIN** 进入页面，在右下角 **设置** 中为“聊天线”和“做事线”排好 DSH 已配置的模型路线（可点“测一下”验证连通性），并按需配置认识的人、微信扫码或飞书长连接账号。
2. **迁移旧版数据（可选）**：若你有旧版 KIN 的家目录，先使用迁移脚本进行只读预演，确认要搬迁的记忆库、人设、既往聊天地址与已送达联系次数无误后，再加 `--apply` 执行写入到独立的 `dataDir`：
   ```bash
   # 从仓库根目录开始；先停掉旧 KIN 与使用同一数据目录的实例，并备份旧数据。
   cd kin
   node scripts/migrate.mjs --from <OLD_KIN_HOME> --to <TARGET_DATA_DIR>
   node scripts/migrate.mjs --from <OLD_KIN_HOME> --to <TARGET_DATA_DIR> --apply
   ```

---

## 验证与测试状态

项目包含完整的本机离线端到端集成测试套件与 Core 单元测试（详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)）：

- **环境基线**：DSH `0.2.2-rc.1`、Node.js `>= 24.9.0`、Python `3.13`。
- **`dsh-kin` 端到端集成测试**：8 套并行场景（`e2e`、`e2e-talk`、`e2e-steward`、`e2e-health`、`e2e-contact`、`e2e-channels`、`e2e-memory`、`e2e-world`）共 **106 项检查全部通过**。
  ```bash
  cd kin
  npm run e2e:all
  ```
- **Core 单元测试**：**1385 项通过、1 项跳过**。
- **真实模型与桌面实测**：升级后的桌面端已完成 DS、OPUS、TERRA 真实上游 API 的连通性验证（回退逻辑由隔离集成测试覆盖）（此前真实 API 场景测试亦在 DS 上跑通闭环），本地 TTS / ASR 语音合成与转写、向量嵌入服务均已实测验证。

---

## 已知边界与当前限制

- **微信群聊暂不支持识别 `@` 提及**：目前微信通道（`kin/src/channels/wechat.ts`）收到群消息时不会标记 `mentioned`，因此微信群内 `@` 她不会触发回复（除非为该群开启了主动“插话”）；需要 `@` 提及触发回复的群聊场景请使用飞书群。
- **长对话下的无向量检索偏重近期消息**：若未配置本地向量嵌入服务，仅靠关键词与近期对话检索时，每轮自动回忆仍偏近期；主动调用 `kin_recall` 时关键词命中优先；建议在实际使用中配好本地向量嵌入服务以获得更均衡的跨周期回忆召回。
- **跨对话共享同一份事实记忆库**：亲密主动面向主人，对话笔记按所属对话读取；事实记忆在同一个心智中贯通（带说话人与对话来源标记），在不同朋友的对话里什么该说、什么不该说由她根据人设与上下文自行拿捏。
