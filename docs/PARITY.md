# 功能对照：旧 KIN → KIN on DSH

> 规则：**只增不减**。旧系统在生产里做过的每一件事，新插件都要接上，或者换成 DSH 原生能力。
> 依据：旧生产路径是 `legacy/host/service.mjs` + `core/adapters/*.mjs` + `legacy/feishu` + `legacy/wechat-acp`（经 ACP 驱动 DSH）。`core/adapters` 已在 Core 瘦身时删掉（说话人与收件人那几件暂留待核对），旧代码看 git 历史。
> `legacy/dsh/src` 那 1.4 万行从没进过生产路径，列在最后，不搬。
> 状态：✅ 已接上 · 🟡 接了一部分 · ⬜ 待接 · 🔁 换成 DSH 原生 · ✖ 不搬。阶段见 [ARCHITECTURE.md](ARCHITECTURE.md)。
> 离线验证：每块由哪套 e2e 证明、最近一次结果、离线验证不了的，见 [VERIFICATION.md](VERIFICATION.md)。

## 1. 心跳与评估（一个每分钟的钟）

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 每分钟的钟：先评估，再联系 | adapters/owner-host.mjs:73 | ✅ 评估这一半在 `heartbeat.ts`；联系那一半是同一个钟上的 `contact` 一道（`kin/src/contact/proactive.ts`） | M4 / M6 |
| 评估轮次：review-due 把关 → review，连续最多 8 轮；ingest / observe / 发送后立刻再看一次 | owner-host.mjs:82-94, :253 | ✅ 她回完一句就再看一次；主动发出、转告送到后也再看一次 | M4 |
| 有活才起模型（review_paused、压缩暂停也在这里） | host/mind-host.mjs:206 | ✅ 常驻进程先答 review-due，有活才起评估进程 | M4 |
| Core 内部的分钟账：愿望到期、计划 tick、空闲评估、无聊唤醒、拆信、愿望保留、探索唤醒 | core host.py:653 | ✅ 每分钟的 review-due 带着记；探索唤醒写下的请求随 M5 去跑 | M4 |
| 她自己定下次什么时候再看（next_review） | memory.py:1017 | ✅ 新库默认开旧 KIN 的开关（idle + semantic…），时间由她每次评估自己定 | M4 |
| 作息相位（rhythm） | rhythm.py | ✅ 新库默认开连续性四件：理解、心事、表达、作息 | M4 |
| 做梦（每晚最多一次，休息时，开关打开） | dreams.py | ✅ 评估在跑，开关在页面 | M4 |
| 日记（评估写下的内心想法，可锁进暗房） | memory.py:1045 | ✅ 评估在跑，页面能看能回 | M4 |
| 拆信（时光信、暗房日记到日子就打开） | sealed.py:268 | ✅ 每分钟拆，打开信箱时也拆 | M4 |
| 每日性格复盘（daily） | mind-host.mjs:220-247 | ✅ 完整评估后待办，有东西可复盘才起；其他等待 15 分钟后再问 | M4 |
| 充实通道：先写“这一段的我们”，空闲或饿了 1 小时再写归档记忆 | mind-host.mjs:270 | ✅ 自己的一分钟，review-due 把关 | M4 |
| 记忆预热（prepare-memory：评估后把深回忆提前压好） | mind-host.mjs:286 | ✅ 每次完整评估后跑；聊天的轻量回忆认得压好的摘要 | M4 |
| 投递 / 工具事件写回记忆（runtime-event）、续上延后的回复 | mind-host.mjs:859 | ✅ 主动、插话、转告送到后写进她的记忆（按收件人）；帮手的活怎么结束的（做完、停下、没做成、断在半路）写成 `task-result`，作品由 Core 的完成复核写成 `artifact-created` / `task-result`，探索结果照旧进记忆；延后回复随 M5 的 choose_reply | M5 / M6 |
| 会话维护：压缩前后的连续性快照、记忆注入确认 | adapters/mobile-session-host.mjs | 🔁 压缩用 DSH 原生；连续性快照待接 | M4 |
| 安静时段（联系一套、提醒一套：22–8 点，每天最多 3 条，间隔 60 分钟） | owner-activity.mjs:61; eventmem scheduler.py:682 | ✅ 页面里的“主动联系”规矩，默认同旧值；主动联系全守，到点提醒和转告只守安静时段（等时段过了再发） | M6 |
| 其他收件人（person_b、家庭群）的心跳 | host/service.mjs:315 | ✅ 联系那一道每分钟替说过话的其他人跑一次他们自己的评估（Core 里按收件人）；群只有插话这一件 | M6 |

## 2. 后台活

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 自主探索（好奇心攒够 → 准备 → 探索，预算 20 分钟） | mind-host.mjs:892-935; codex_executor.py | ✅ 她的评估留下探索唤醒，心跳的探索通道去跑；执行换成 DSH 帮手（做事线，DSH 的搜索和读网页），Core 新的 `dsh` 后端经本机回环口交给插件（Codex 执行器已删，`dsh` 是唯一后端）；帮手读过的网页记成来源回执，引用照旧核对；出门页能看正在探索什么、上次带回什么，能叫她回来 | M5 |
| 探索结果修复 | host.py:754 | ✅ 沿用 Core 原路：结果格式不对时做一次只修格式的修复（做事线），没有回执撑着的引用照旧丢掉，断了留检查点下次接着 | M5 |
| 自主创作（计划领取、续租、结果复核） | adapters/autonomous-creator.mjs | ✅ 心跳的创作通道：plan-claim → DSH 帮手在这一步自己的工作区里做（做事线）→ 每 20 秒续租 → Core 完成复核（plan-result，没空位就保着租约再问）→ 做不完带原因还给她的计划；心愿页能看正在做什么、上次做好了什么、放在哪，能先停下。静态 HTML 渲染核验（creation-verifier）没搬：要 Playwright，帮手用自己的工具核验 | M5 |
| 计划（autonomous plans） | plans.py | ✅ 页面能看；聊天里她用 Core 的计划工具自己建、改、暂停、取消，引用这句话的来源编号 | M5 |
| 评估队列、反复失败的隔离 | host.py:514-558 | ✅ 心跳在消费 | M4 |
| 这一段的我们（window notes） | window_notes.py | ✅ 充实通道写，开关在页面 | M4 |
| 归档记忆（用她自己的话记住归档的愿望） | archive_memory.py | ✅ 充实通道写 | M4 |
| eventmem 工作者与维护（整理、叙事日记、生命周期、提醒调度） | eventmem jobs.py:836-861 | ✅ 常驻进程，停了半分钟后再起；向量嵌入要本地嵌入服务，迁移时带上配置，没有时按词检索 | M4 |
| 启动恢复（recover、plan-recover，只打断确实没了的探索） | mind-host.mjs:956-971 | ✅ 心跳开始前先跑 | M4 |
| 上一代宿主留下的孤儿进程 | mind-host.mjs:952 | 🔁 帮手都是 DSH 会话，随 DSH 收尾；插件关掉时探索、创作一起停；重启后还记着“在做”的活标成断在半路，在提起它的那段对话里告诉她（帮手会话还在，kin_work_tell 一句就能接着做），探索、创作由 recover / plan-recover 收回 | M5 |
| 记忆出件箱恢复 | mind-host.mjs:841 | ✅ 渠道来的话一到就入库；发出去的每一条先落盘（`deliveries.json`），没写进记忆、没和 Core 结清的每分钟再补 | M6 |
| 已删除来源的清扫（每小时） | duo-host.mjs:15 | ✅ 每小时问 Core `deleted-sources`，等着进会话的连发、群里没回的话里，来源删了的就丢掉 | M6 |
| 运维命令（maintenance-tick、vector-optimize…） | host.py:379-445 | 🔁 Core 瘦身时删掉了 maintenance-tick、vector-optimize（开关默认关，关着时只报告、不动库）和各种修复 / 回滚命令；留下的仍是 Core 命令行、手动跑：旧库迁移（migrate-evidence-isolation、slim-evidence-refs、traits-migrate、plan-migrate…）、历史压缩（history-*）、愿望 / 探索决定归档、`appraisal-triage`（“她的状况”里指过去的）、`python -m eventmem.core.repair` | — |
| 语音文件 14 天后清理 | speech.mjs | ✅ 每天看一次，满 14 天的挪进废纸篓（不直接删，和以前一样） | M6 |

## 3. 主动联系与投递

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 联系流水线：claim → 起草 → check → 存待发 → 发 → settle | owner-host.mjs:140-264 | ✅ `kin/src/contact/proactive.ts`，每一步都在那个人自己的视角里（`recipient_person_id`）；`kin_reply` 的“晚点再回”由原来的对话接回，Core 不把它交给主动联系 | M6 |
| 能不能联系（关了、安静时段、等回复、刚聊过）；已开始的一条要发完 | feishu/contact.mjs:97 | ✅ 外加每天上限；开了头的一条接着发完（开关和安静时段照样拦） | M6 |
| 按收件人判断（没关主动、还没认识、群只在非 @ 模式） | contact.mjs:132 | ✅ 每个人一个“她可以主动找”，群一个“没人 @ 也可以插话”；没说过话的不找 | M6 |
| 起草（她的口吻，聊天线；失败换模型重试一次） | mind-host.mjs:653-725 | ✅ 人设 + 那个人视角里的回忆和最近的对话 + 就绪的心愿，走聊天线；失败或答不成形，换聊天线的下一条路线再起草一次 | M6 |
| 连续技术失败暂停 120 分钟 | contact-pause.mjs | ✅ 连着失败就停，1、2、4…分钟，最长 120 分钟；成功一次就恢复 | M6 |
| 发送闸门与分享检查 | mind-host.mjs:120; sharing-preflight | 🟡 每一段发出前再问规矩、她是不是正在回话、对方刚才有没有开口；草稿引用了发现时过 `sharing-preflight`（按收件人记住分享过什么）；没有引用的不预检 | M6 |
| 投递状态：待发、平台已收、未确认、发前取消、待复核；永不换 id 重发 | owner-host.mjs:117-254 | ✅ `deliveries.json`：一条一个 id，每一段 `<id>:<n>`；回复、主动、插话、转告、提醒、系统通知都走它 | M6 |
| 结果未知的核对（5 分钟起，翻倍到 6 小时） | state.py:2234 | ✅ 按那一段自己的 id 问平台；送到了就接着发剩下的，平台说没到才用同一个 id 再发 | M6 |
| 延后回复（choose_reply：回 / 不回 / 合并 / 晚点） | mobile-routing.mjs:280; habits.py | ✅ 她自己选（`kin_reply`）：不回、晚点、等下一句一起回，这一轮就不出声；选择记在 Core，要主人在设置里放手（累的时候可以晚点）。晚点的到点回到原来的对话里接着回，Core 的接回愿望随之了结 | M5 |
| 家庭群要不要插话（每分钟） | companion-contact.mjs | ✅ 开了插话的群：每分钟把没回过的新话交给她起草，她决定说或不说 | M6 |
| 提醒、问候、转达（eventmem 调度 + 签名回调） | scheduler.py; task-delivery.mjs | 🔁 提醒用预设里 DSH 的 schedule：到点进那个会话，她的回话照常发出（守安静时段）；转达是 `kin_tell` 工具；问候就是她的主动联系，不再有固定问候语 | M6 |
| 把主动发出去的话告诉聊天会话 | outward-effects.mjs | ✅ 送到后作为她的话（`kin-said`）记进那个会话；桌面会话就是这样收到她主动发的消息 | M6 |
| 桌面任务交换 | task-exchange-host.mjs | 🔁 DSH 会话本身就在桌面 | — |
| “为什么没联系”：状态与原因可见；48h 没新愿望 / 72h 没探索 / 72h 没联系告警 | mind-host.mjs:773; health.mjs:167 | ✅ 此刻页“主动联系”：每个人这一分钟为什么没找、上次什么时候找的、最近发了什么；三条告警 | M6 |

## 4. 渠道

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 桌面聊天 | file-chat.mjs（桌面 md 文件） | ✅ KIN 页面自己的聊天框，边打字边出字 | — |
| 飞书收（推送 + 每 15 秒补拉 24 小时） | feishu/listener.mjs | ✅ 长连接推送（不用飞书 SDK，自己解帧）+ 每 15 秒把认识的会话从上次补齐处往后拉（最多回看一天），按消息 id 去重；`kin/src/channels/feishu.ts` | M6 |
| 飞书附件（图片、文件、预览） | listener.mjs:152 | ✅ 图片、文件、语音、视频存进她的数据目录（只有主人能读）；不是 PNG / JPEG / WebP 或太大的图，另做一张 JPEG 预览；下载失败的那件在消息里写明 | M6 |
| 飞书发（文字、图片、文件、语音） | feishu/sender.mjs | ✅ 每条按投递 id 带固定 uuid，重试认得出；结果未知的，一小时内原样再问一次（同 uuid 只会成一条），过了就如实说没法确认 | M6 |
| 微信收发（长轮询、扫码登录、正在输入） | wechat-acp/src/weixin | ✅ 设置页里扫码登录（二维码画在页面上），长轮询收，正在输入；登录存在她的数据目录（只有主人能读）；`kin/src/channels/wechat.ts` | M6 |
| 微信媒体（语音转写、图片、视频、文件） | wechat-acp adapter/inbound.ts | ✅ 收：图片、语音（带微信的转写）、视频、文件解密后存盘；发：图片照发，语音和文件按文件发（和以前一样） | M6 |
| 微信发文件（只发主人点名的文件） | explicit-wechat.mjs | ✅ 通道这头不发没标 `named` 的文件、视频；她发的话里只有主人在那段对话里点名要的那个文件才标 `named` | M6 |
| 20 秒内的连发算一条 | owner-input-batch.mjs | ✅ 每条都等 20 秒安静；群里 @ 她的马上走；落盘，重启后接着送 | M6 |
| 字面停止（停止任务 / 取消当前任务 / /停） | mind-host.mjs:875 | ✅ 渠道里发这三句，丢掉还没送进去的话，停下她在那个会话里正做的事；页面有“停一下” | M6 |
| 谁在说话（飞书 app + open id → 人；群里 @ 才回） | companion-routing.mjs | ✅ 平台账号 → 人；不认识的人不进会话，页面上可以一键认下 | M6 |
| 群聊：普通群话只观察 | duo-host.mjs:58 | ✅ 记进她的记忆，不起回合；下次有人 @ 她时连同前面的话一起给她 | M6 |
| 转达、提醒、偏好 | duo-host.mjs:117 | 🟡 转达（`kin_tell`）、提醒（DSH schedule）✅；对方的关系、话题偏好这些 Core 动作随 M5 挂 Core 工具 | M6 |
| 每个收件人看到的状态不同（亲密内容只给主人） | companion_host.py; companion_view.py | ✅ 回忆、入库、起草、评估、她聊天时用的 Core 工具和私人笔记都带着会话的收件人，Core 在那个人的视角里答；亲密的只在主人自己的对话里 | M6 |
| 写死的收件人 person_a / person_b / group:family | companions.py:12 | ✅ 设置页“她认识的人”：人和群、各平台账号；Core 只认 `person:<id>` / `group:<id>` 的格式，主人仍是 person_a | M6 |
| 分享护栏（私密来源不外泄、记住分享过什么） | host.py:345 | 🟡 分享过什么按收件人记（送到的投递事件、`sharing-preflight`）；别人的对话不进起草；来源是否给别人看仍由她判断（Core 的设计） | M6 |
| 语音、表情包、图片 | speech.mjs; meme-host.mjs | ✅ 本机语音模型合成（同一条只合成一次，设置页填路径）、按意思从表情库挑图、图片照发；发不发、什么时候发由她自己定：`kin_voice` 说一句语音，`kin_sticker` 按此刻的意思挑一张，发到她正在聊的那段对话；桌面聊天里表情直接显示、语音能播放，说过的话进她的记忆 | M6 |

## 5. 模型

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 两种路由：聊天、干活 | model-config.mjs | ✅ 聊天线 `kin/chat`、做事线 `kin/task`，页面里拖动排序 | — |
| 聊天回复回退（最多 4 条，只在上游失败时换） | main-reply-fallback.mjs | ✅ 条数不限；还没开口就出错或一分钟没回应就换 | — |
| 13 个记忆角色（压缩、回忆、看图…） | model-config.mjs:21 | ✅ 都走做事线：插件在本机回环口开一个 Messages 接口（`kin/src/bridge.ts`），Core 的模型文件指向它，密钥留在 DSH | M4 |
| 后台并发上限（默认 2、最多 4，聊天优先） | model_lanes.py | ✅ 上限 2 写进配置；她回话时心跳不起新活，回忆过后 3 分钟内后台让路（Core 的前台租约） | M4 |
| 评估用的模型（聊天会话的分身，带人格） | host.py:102; mind-host.mjs:154 | ✅ 走做事线；人设同步进 Core 的 persona-policy.json，评估、日记、梦都用她的口吻；回忆用 Core 自带的读记忆工具 | M4 |
| 输入分类（聊天 / 干活 / 停止） | mobile-reviewer.mjs | 🔁 她在对话里自己决定派活 | — |
| 各动作超时（评估 660 秒、探索 1500 秒…） | mind-host.mjs:295 | ✅ 会调模型的动作各起一个进程，按旧表设超时；常驻进程的短动作 20 秒 | M4 |
| 模型设置页 | model-config-web.mjs（8322 端口） | ✅ KIN 页面里；密钥在 DSH | — |
| 连通性测试 | model-config-web.mjs | ✅ 每条路线一个“测一下”：只走这一条发个很短的请求，显示通没通、用了多久、为什么没通 | M5 |

## 6. 其他

| 功能 | 旧位置 | 现在 | 阶段 |
|---|---|---|---|
| 后台并行任务（start_background_task…） | parallel-task-host.mjs | 🔁 主人的活由她用 `kin_delegate` 交给帮手：主人那个文件夹里一个自己的 DSH 会话（主人的 standard 预设，走做事线），能做到哪一步（DSH 的沙盒和审批）按主人在设置里选的“帮手的权限”；文件夹要绝对路径、不能是她的数据目录，不清楚就先问。做完结果回到提起这件事的那段对话，她用自己的话讲；`kin_work_tell` 补一句或让它接着做，`kin_work_stop` 叫停；页面“此刻”有“手头的活”（在做什么、在哪个文件夹、停下）。对话里顺手的小事她用 DSH 的 subagent（standard 预设、做事线）。“帮手的权限”只列这个 DSH 真有的档：问我（workspace-write，越界在 DSH 里问主人）/ 自动审核（DSH 的 Auto review，要在插件里打开）/ 全权（danger-full-access）；没选时全权。它用在新开的帮手会话、她自己的对话（每轮开始时设上，subagent 从对话继承）；她自己出门、创作的帮手不跟这一档，只在她自己的目录里动手；改了只管之后的活 | ✅ M5 |
| 进度小结（开着的活） | work-lock-review.mjs | ✅ 她用 `kin_work` 看手头的活：在做的做到哪了（帮手最近说的、在用的工具、在等主人批准）、最近做完的结果、各在哪个文件夹 | M5 |
| 输入看护（没进会话的输入重排） | mobile-routing.mjs:1277 | 🔁 DSH 会话收件箱 | — |
| 自检（每 4 小时）、故障看护（每 5 分钟） | mobile-audit.mjs; fault-watch.mjs | ✅ 插件定时器（`kin/src/health/`），只读、不调模型：5 分钟看进程、中转、两条模型线、心跳、旧宿主的存活规则和渠道；4 小时再看留下的活和心智进程。设置页“她的状况”写哪里不对、从何时起、怎么办；DSH 日志记出问题和恢复。旧自检的模型复读换成规则 | M7 |
| 系统通知（【系统通知】） | owner-notice.mjs | ✅ 主人在渠道里的回复没完成、结果未知、只送到一部分，各发一次固定的【系统通知】（不是她的话，不进记忆） | M6 |
| 聊天时的工具：宿主约 25 个、心智 24 个、eventmem 若干 | kin_memory_mcp.py; core mcp.py; eventmem mcp.py | ✅ Core 的 27 件原样经 DSH 的 MCP 工具桥挂上、在常驻心智里跑（作用域固定，不进说明；调错了 Core 会说哪里不对）：计划、愿望、心事、主人的习惯；她的状态、性格、自我认识与预测；人和事的图谱、事件来龙去脉、原文、旧记录、话题、做法、分享过的和做过的；记住、不再主动提、改正记错的；私人笔记、梦、日记、时光信。她自己的 `kin_recall`（带编号，能接着往下查）、`kin_note`、`kin_reply`、`kin_persona`。不挂：回想和逐条评估（`kin_recall` 和后台评估已在做）、提醒与联系任务（随 M6）、回复引用（要投递账本）、运维与删除 | M5 |
| 设置：configure-memory / behavior / contact / autonomy / habits / continuity… | host.py | 🟡 内心开关 ✅；“她自己拿主意”✅：回不回由她（habits，你在页面上的选择记成你说的话）、会累（精力）；behavior / autonomy / continuity 按旧 KIN 起步，不进页面；联系：她认识的人、主动联系的规矩 ✅ | M5–M6 |
| 联系偏好（等回复） | contact_preferences.py | ✅ 设置页“等回了再找”；同时写进 Core 读的 `proactive-policy.json`，她评估时知道 | M6 |
| 人设 | prompt_store.py | ✅ 页面里改，下一句话生效；她自己通过 `kin_persona` 保存后直接生效，聊天与后台使用同一份，旧版本留存 | M5 |
| 精力、私人笔记 | energy.py; private_notes.py | ✅ 精力默认关，设置里“会累”打开；她每回一句记一笔（消耗和恢复 Core 算，也算进无聊感）。私人笔记 `kin_note` 记，回想时一起想起，评估也读 | M5 |
| 日历 / 待办视图 | HANDOFF“尚未完成” | ✖ 旧系统也没做 | — |
| 健康与运维（/busy、/freeze、/thaw…） | health.mjs; mobile-routing.mjs | 🔁 设置页“她的状况”：冻结 / 解冻她的后台（心跳全停，主动联系随之停，记忆工作者歇着；重启后还冻着；聊天照常）、此刻在忙什么（原 /busy）、手动自检。发布用的排空不搬：插件重载归 DSH | M7 |
| 每日检查（launchd，每天两次） | daily-check.mjs | ✅ 插件定时器，6:40、18:40，错过的开着后补：完整自检，加磁盘、数据目录、过去一天的评估；页面留最近 14 次。升级、备份随 launchd 不搬；连续没通过只写在页面和日志 | M7 |
| 旧状态文件迁移（出件箱、联系状态、收件人、联系策略…） | migrate-kin-home.mjs | 🟡 `kin/scripts/migrate.mjs --from <旧 KIN> --to <数据目录>`：默认只预演（搬什么到哪、什么不搬为什么、页面上要重做什么），`--apply` 才写；旧目录只读，要替换的先挪进 `before-migrate-<时间>/`，重跑不重复写。心智库整份复制并带上旧的作用域；人设、她认识的人、联系规矩、声音、表情库照搬；Core 里还挂着的主动消息按原编号记进 `deliveries.json`（没发的取消、心愿还在；开始发的待确认、不再发）；微信登录、飞书密钥、模型线在页面上重做。写好了，没在任何数据上跑过，真实数据由主人来跑 | M7 |

## 7. 不搬：旧代码里就没进生产

`legacy/dsh/src` 的服务层（createKinService）、唤醒调度、出件箱 / 发送策略 / 通知策略、模型路线与回退流、Google 日历、cutover / migration / upgrade / self-iteration / sa-bridge / task-ledger、persona-store / memory-palace / context-bridge 等，只被测试引用；Codex 的每日复盘自动化；运行时包升级；原生会话轮换；微信提醒旧路径与退役的 launchd 标签。

## 搬的时候最容易出事的 5 件

1. **重复发送**：结果未知的一条只能按自己的 id 核对（5 分钟 → 6 小时），不能重新起草；开始了的一条要把几段都发完。
2. **一个钟**：把关、分钟账、她自己定的下次评估、每日复盘、充实通道都挂在同一个分钟钟上。拆成几个独立定时器，她要么醒不来（没有空闲评估、梦、日记、拆信），要么每分钟烧一次模型。主人正在聊时要让路，后台并发要限住。
3. **收件人范围**：收件人要一路带进 memory-context、observe、ingest、claim，否则私密内容串到别人那里，或者主动消息发错人。
4. **起草的口吻**：旧的起草和评估是聊天会话带人格的分身。换成不带人格和回忆的通用任务模型，她主动说话就不像她了；放到聊天线又不限并发，后台会挤掉她回你的话。
5. **重启**：DSH 插件重载比旧服务频繁。recover / plan-recover、续上起草中的联系、只打断确实没了的探索、记忆日志和出件箱，都要有持久的落脚处，不然会丢发、重发、留下孤儿探索。
