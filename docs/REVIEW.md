# KIN 代码审查：Codex 交接快照（2026-11-05）

> 结论先行：Codex 这一周没有朝目标走。目标是 **DSH 当基座、KIN 作为常驻全局插件**；实际做出来的是 **老 Host 继续当基座，DSH 被降级成 ACP 子进程里的模型执行器**，外加一套不接生产的平行“KIN 服务”。值得留下的是 Python 心智、微信/飞书协议层和约 3 千行业务行为，其余推倒重来。

## 1. 体量与处置

| 模块 | 非测试代码 | 测试 | 处置 |
|---|---|---|---|
| `host/` + `core/adapters/`（老宿主） | 41.8K 行 mjs + 1.5K 行 py | 33K+ 行 | 退役。约 3K 行行为移植进插件，约 20K 行在 DSH 当基座后作废，约 18K 行纯冗余 |
| `dsh/`（Codex 新写的 DSH 层） | 23.4K 行 | 13.9K 行 | 推倒。生产实际只加载其中约 2.7K 行（ACP 桥） |
| `core/` Python 心智 | 60K 行 | 42K 行 | 保留心智，接口要改；约 29K 行是修复/迁移/Codex 时代基础设施，后续瘦身 |
| `core/console` + `core/sdk` | 11.8K 行 | — | 记忆运维台，不是陪伴界面；只借日记/梦、时间线、配色 |
| `wechat-acp/` | 8.7K 行 | 8.3K 行 | 只留 `weixin/*` 协议层 + 入站转换 + 游标存储（约 1.8K 行），ACP 侧丢弃 |
| `feishu/` | 1.4K 行 | 2.0K 行 | 留 listener / catch-up / sender / keychain / group-members |

测试与代码比：host 1.15 : 1，feishu 1.47 : 1，dsh 0.6 : 1（而且大部分在测死代码）。

## 2. 主要问题

### P0 · 架构做反了
- Host 给每个联系人生成一个 DSH profile，再把 `dsh --profile kin-xxx` 当 ACP 子进程拉起，只用它跑模型（`host/native-runtime.mjs`、`host/native-dsh-launch.mjs`）。DSH 的会话、调度、设置、前端都没用上。
- 同一条消息要穿过：微信长轮询 → KinBridge（2,006 行的第三方桥子类）→ 写成任务文件 → 同进程再读回来 → MobileRouter（2,813 行的第二套会话管理器）→ ACP → DSH。
- 桌面 KIN 页面是另一个独立插件，和 Host 里的 KIN 互不相通。

### P0 · 平行造轮子，大部分不接生产
- `dsh/src` 1.4 万行，文件头写明 *does not import a DSH package*：自建 outbox、任务账本、调度器、持久化、Web 网关与准入、模型路由策略、通知策略、执行器策略、Super Agent 桥（758 行，带 HMAC/mTLS、游标、intentVersion）、自迭代、切换/迁移/升级，缺依赖时用 Fake 顶上。
- 从生产入口追 import：Host 只加载 `kin-dsh-host/acp`（约 2.7K 行），完全不经过 KIN 服务；`model-policy`、`notification-policy`、`memory-palace`、`host-bridge`、`executor-policy` 等没有任何生产入口引用，只被测试养着。
- 同一件事多处各写一遍：常驻动作清单 ×3（`host.py`、`mind-host.mjs`、`kin-core-contracts.ts`），原子写文件 ×4，停止指令正则 ×3，路径解析 ×3，模型路由 ×3（Host、`dsh/src/model-policy.ts`、DSH 自带 LLM 层）。
- `scripts/patch-native-fast.mjs` 在 postinstall 时直接改 `node_modules` 里 DSH 包的源码。

### P1 · 桌面 KIN 页面是调试面板
- 侧栏 8 个 tab，只接 4 个只读 GET，每读一次新起一个 Python 进程（0.6–1.3 秒）。
- 聊天输入框禁用；DSH 自带的原生对话界面没用，自己写了个空壳 ChatPanel。
- 直接显示“身份：Host 绑定 / 模型：Host 侧选择 / rev 3 / 来源 uuid · uuid”；28 维情绪原样平铺；心情和自主两个 tab 内容重复；只用系统色，没用 DSH 的 UI 组件。

### P1 · Core 心智好用，但接口不适合直接挂 DSH
- 每轮都要用的 `memory-context` 不在常驻模式里，只能每次起进程（约 1.2 秒）；常驻模式里 `read` 约 40ms。
- 输出很“脏”：JSON 套 JSON，28 维情绪，还会算出假心率/呼吸（`affect_layers.py`）。
- 收件人写死成 `person:person_a`、`person:person_b`、`group:family`，33 处。
- 几乎所有状态由一次结构化 LLM 调用写入（`Appraisals.run_one`，单个方法约 1,430 行，一次产出约 25 个分区）。
- `schema.sql` 漏了 19 张首次使用才建的表。
- 合成环境下跑了 15 个测试文件：331 个过 324 个，7 个失败均来自脱敏占位和离线环境（tokenizer 缓存、占位域名），不是产品缺陷。

### P1 · 渠道
- 微信只认扫码登录的主人，其他人静默丢弃，群消息直接丢。用户乙和家庭群只走飞书。
- 说话人识别 `CompanionDirectory.receive` 本身只有 67 行，但每条飞书消息要重复推导 3 次，目录文件被 19 处反复读盘校验。
- 一条消息可能经过约 10 种回执/账本目录（shared-input-receipts、inject pending/processing/done/failed、send-receipts、飞书 outbox、transport-manifest、contact-batches、task-delivery 五个文件夹……），回执还是靠替换全局 `fetch` 拿到的。

### P2 · 过度防御、过度测试、噪音
- 发版/部署工具约 11.6K 行（`kin-deploy.mjs` 一个文件 5,979 行）外加 11.9K 行测试，服务对象是一台 Mac。
- host 运行代码约 149 个空 `catch`；25 个 `setInterval` 轮询循环；注释里 1,184 处工单号（如 `CR2-LIFE-08`），core 里另有 310 处。
- 测试按字符串锚点从源码里抠代码再 `Function()` 执行（`host/input-replies.test.mjs`、`host/reply-manifest.test.mjs`），改个缩进就挂。
- dsh 层注释里 154 处 “never / must not / does not” 式防御声明。

## 3. 主动联系相关的硬编码（重写时逐个重新判断）
- 可配置：`proactive-policy.json` 的开关、时区、安静时段、等回复、最小间隔小时数。
- 写死：心智循环 60 秒；联系暂停默认 120 分钟、退避 60 秒·2^n；未发出联系的窗口 24 小时；用户乙默认 `proactive:false` 且必须对方先开口；群需 `mention_only===false`；所有主人消息无论单条都要等 20 秒合批；签到最多 3 次/天且默认关闭；提醒 100 次/天；探索预算 1200 秒、重试 1800 秒；群插话 60 秒一轮；回合上限 30 分钟。
- 没有发现“每条消息人工审批”的门。

## 4. 值得保留的东西
- **Python 心智**（记忆、图谱、情绪评估、心愿、计划、联系任务、节律、日记/梦）：概念和数据模型都在，接口要改造。
- **`core/dsh-plugin`（dsh-eventmem）的挂钩方式**：`agent/created`、`session/event`、`tools/result`、`agent/status` + `agent.inject()` 注入回忆，这才是 DSH 原生集成的正确写法（但它喂的是通用编码记忆，不是 KIN）。
- **Core 的 `main_session_review` 模式**：评估时把 LLM 请求交给宿主执行，正好让 DSH 用 KIN 的模型路线（含回退）来跑。
- **约 3K 行业务行为**：说话人识别与按会话串行、消息合批、附件、TTS、回复信封与分气泡、心智循环与联系流程、提醒。
- **协议层**：微信 `weixin/*`（登录二维码、长轮询游标、AES 媒体、CDN 上传发送）、飞书 listener/sender（含断线补拉、uuid 幂等）。
