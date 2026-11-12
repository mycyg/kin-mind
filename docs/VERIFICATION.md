# 验证记录

> 下文保留历次结果；最新本机验证为 113 项集成通过、Core 1415 通过 / 1 跳过、Node 10 通过，并完成真实数据快照与现用模型 API 验证。旧版数量与待办不是当前状态，详见末尾补充。

> M7 的交付物。全部在本机离线跑：真实的无头 DSH 0.2.1-rc.5 + dsh-kin + 她真实的 Python 心智（合成数据），模型由脚本扮演，微信 / 飞书是 127.0.0.1 上的假平台，她的微信、飞书传输层是真的。等待按开发节奏 `pace 0.05` 过（她的一分钟约 3 秒）。
> 没碰真实模型、真实账号、真实数据；各套件只读写仓库里的 `.dev/`。功能对照见 [PARITY.md](PARITY.md)，施工顺序见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 怎么跑

```bash
cd kin
npm run e2e:all                 # 先构建，再并行跑 dev/ 下所有 e2e*.mjs，打一张表；有一项没过就非零退出
npm run e2e:world               # 单跑一套：e2e、e2e:talk、e2e:steward、e2e:health、e2e:contact、e2e:channels、e2e:world
KIN_PACE=1 npm run e2e:world    # 按她真实的节奏跑（慢很多：评估要等对话安静三分钟，未知结果五分钟后才核对）
```

用官方 Node 24（仓库里 `.dev/node24/bin`），不要用桌面版自带的 Node。每套的完整输出在 `.dev/e2e-all/<套件>.log`，数据和 DSH 日志在 `.dev/<套件>/`。以后新加的 `dev/e2e-*.mjs` 会自动进 `e2e:all`。

## 最近一次 `e2e:all`（2026-11-05，本机，八套并行）

| 套件 | 通过 | 失败 | 用时 |
|---|---:|---:|---:|
| e2e | 21 | 0 | 43 s |
| e2e-talk | 12 | 0 | 37 s |
| e2e-steward | 12 | 0 | 40 s |
| e2e-health | 6 | 0 | 61 s |
| e2e-contact | 20 | 0 | 71 s |
| e2e-channels | 16 | 0 | 41 s |
| e2e-memory | 6 | 0 | 50 s |
| e2e-world | 12 | 0 | 74 s |
| **合计（并行）** | **105** | **0** | **74 s** |

连跑三次都全过，总用时 71–130 秒：最慢的是 e2e-contact，它等那条未知消息的核对按 5、10、20 分钟（开发节奏下 15、30、60 秒）翻倍，赶上哪一档就差出一分钟。

## 每套证明什么

| 套件 | 证明什么 | PARITY |
|---|---|---|
| `e2e` | KIN 预设只管 KIN 会话：人格、回忆进请求，对话入库，换个会话还记得；编码会话不受影响、不入库。聊天线第一条路线不存在时换到第二条。各页面路由。省 token：聊天少想、不带 todo / job、心情不变不重复说。心跳评估走做事线、带人设、改了心情 | 1、5 |
| `e2e-talk` | 对话里的工具：Core 的计划、愿望（调错会说哪里不对），私人笔记和回想时想起，“晚点再回”到点接回，人设改动直接生效，回一句耗精力，主人封的时光信，每条路线“测一下” | 3（延后回复）、5、6 |
| `e2e-steward` | `kin_delegate`：帮手在主人文件夹里干活、走做事线、按“帮手的权限”（Auto），结果回到原对话、她用自己的话讲、进记忆；subagent 在 Auto 下写文件；没给文件夹不开工；上次留下的活标成断在半路；探索、创作在她的分钟里跑完 | 2、6 |
| `e2e-health` | 做事线不通、记忆工作者停了，页面写哪里不对和怎么办，DSH 日志也有；冻结停心跳和工作者，重启后仍冻结，解冻恢复 | 6 |
| `e2e-contact` | 20 秒连发算一条、群里 @ 才回并带上前面的话、转告、主动找主人和朋友（各在自己的视角）、结果未知按原 id 核对不重新起草、亲密只给主人、安静时段、页面认人、主人的图片和点名要的文件 | 1、3、4 |
| `e2e-channels` | 飞书：推送、补拉、附件、去重，回执丢了按 uuid 核对只成一条；微信：扫码、长轮询、媒体解密、只发点名文件、正在输入；语音合成和 14 天清理；表情包；DSH 里从保存的账号启动、密钥不上页面、别的插件经 `kinTransports` 收到消息 | 4 |
| `e2e-memory` | 多人同时私聊加群：每句话记在说话的人和那段对话名下（Core 存的说话人、对话、图谱里的发起人）；评估知道编号是谁、话是什么时候说的；在小红的对话里想起时写明是小美 / 主人私下说的；每条回想带本地时间戳（含星期）；回忆页“记错了”的更正留在那条记忆上 | 4、6 |
| `e2e-world` | 跨模块的一个故事，见下节 | 1–6 合起来 |

## 合成场景（`e2e-world`）

人物：主人小光（桌面页面 + 微信），朋友小红（飞书），家庭群（飞书）。她的聊天线第一条路线是坏的。中途主人冻结她、DSH 停掉（这时有一条结果未知的转告、一件还在做的活），再在同一份数据上启动。

| 检查 | 怎么看 |
|---|---|
| 跨渠道：主人在微信说的，她在桌面知道 | 微信说“下周三去杭州出差”，桌面问“去哪儿出差”，她答出杭州 |
| 心跳：聊完之后的评估看到了微信上的话 | 做事线收到的评估里有这句 |
| 心跳：她主动找主人，发到主人最后说话的地方（微信） | 假微信收到她的话 |
| 只属于主人的（亲密、私人笔记）到得了主人，到不了小红 | 她给主人起草时带着色色值，桌面回想带出她在微信里记的私人笔记；小红问起，她在小红的对话里自动回忆和 `kin_recall` 都没有这两样 |
| 家庭群：普通的话只听不回，@ 她才回 | 假飞书群里先没有回复，@ 后有 |
| 大总管：主人在微信让她整理文件夹，帮手做完，结果回到主人的微信 | 文件写好、账本里做完、微信收到她的转述 |
| 重启：重启前的冻结仍在，什么都没跑，未知那条原样等着 | 健康页冻结、这次还没跳过一分钟、投递仍是未知 |
| 重启：被打断的活标成断在半路，她在微信告诉主人 | 账本、微信 |
| 重启：解冻后她的分钟又跑起来 | 联系那一道在解冻后有了新的一轮 |
| 重启：未知的转告按自己的 id 核对，飞书上只有一条 | 投递变成已送达，没有第二条投递，平台上一条 |
| 重启：之前说过的话一条都没重发 | 重启后微信只多了她说活断了那一句，飞书一条没多 |
| 回退：第一条路线不通，第二条接上 | 设置页的聊天线：nowhere 失败，kin-test 接上 |

## Claude 交接时尚未做的真实验证（后续结果见文末）

- **真实模型和密钥**：两条线在真实上游上的回退（离线只验了“路线不存在”；一分钟没回应换路、开口后出错不换，没在真实上游上看过），思考深度和费用。
- **真实微信 / 飞书**：页面里填真实地址、扫码登录；飞书应用的长连接（事件订阅）和拉消息、发消息、上传的权限；平台真实的回执和去重。
- **微信群**：假平台上没法验证 iLink 推不推群消息、@ 长什么样；目前微信群里 @ 她不会回（见下面“发现”1），场景里的群放在飞书。
- **语音模型**：本机语音合成只用假的 worker 走过协议，真实模型、ffmpeg、发出去的语音条要听一听。
- **DSH 的 Auto review 插件**：开发组合里打开过；桌面版要在 Plugins 里打开，“帮手的权限”才会多一档“自动审核”。
- **迁移真实的旧 KIN 家目录**：`kin/scripts/migrate.mjs` 还没有离线套件跑过；真实数据由主人来跑，先预演、看清搬什么，再 `--apply`。
- **1M 上下文下的 token 预算**：只量过短会话的固定部分；长聊、DSH 压缩后回忆续上、800 token 回忆预算，要在真实长对话里看。
- **向量嵌入**：离线没有嵌入服务，回忆只按词和最近；配上本地嵌入服务后回忆的质量要看（见“发现”2）。
- **真实节奏**：所有等待按 0.05 倍过；`KIN_PACE=1` 能跑，但真实的作息、安静时段、每日检查 6:40 / 18:40 只能在日常使用里看。

## 这次跑出来的发现

1. **微信群里 @ 她不会回。** `kin/src/channels/wechat.ts` 收消息时从不设 `mentioned`，群里的话一律只听（除非这个群开了“插话”）。旧 KIN 的微信只收私聊，家庭群在飞书。建议：拿到真实 iLink 群消息的样子后在 `wechat.ts` 认出 @；在那之前，页面上说明微信群只能靠“插话”。
2. **离线时回忆偏“最近”。** 场景里，桌面那一轮的自动回忆没带出微信上的话，是她用 `kin_recall` 时最近几条带出来的。再多聊几句、评估过后，“下周三去杭州出差”虽然按词能搜到（Core 的全文索引命中），`kin_recall` 第一页的 8 条却被最近的对话占满，桌面的自动回忆什么也没给。建议 Core 给按词命中的留几个位置（或 `kin_recall` 用 deep），配上嵌入后再看一次。
3. **记忆是一份。** 主人说“别告诉别人”的原话，在小红的对话里她能回想到；按人分开的只有亲密和她的私人笔记（Core 的设计），说不说由她判断。主人需要知道这一点。
4. **e2e-health 在七套并行时会抖。** 开机时的每日检查在高负载下要六秒多，比被杀掉的记忆工作者重启（0.2 倍节奏下六秒）还慢。已把这套的节奏下限从 0.2 调到 0.5（工作者停十五秒），连跑三次全过。

## 真实旧库与桌面验收（2026-11-05）

八套集成测试105项通过。真实模型验证覆盖 DS MAX 的跨渠道对话、记忆工具与后台评估，以及 ARGON medium、OPUS 5.5 medium、Terra MAX 的完整响应。本地 TTS/ASR、1024维向量服务和原生桌面均已实测。

旧数据发现并修复：已编辑人设覆盖新同步、主人编号迁移、状态文字被前端丢弃、空模型流不回退、本地服务地址误脱敏、缺少语音转写。人设更新不再需要批准。

Core 在配置好 tokenizer 缓存并恢复协议测试地址后：1368通过、17失败、1跳过。剩余是旧文档/提交缺失、失效的脱敏样例与固定联系人断言，不能将其报告成全绿。私有运行记录、具体备份位置与真实对话样本保存在部署数据目录，未写进源码。


补充根因：迁移恢复既往聊天地址与已送达联系次数；模型未提交输出前的不完整工具调用可回退；计划默认查询仅 active/paused，历史不再挤占常规评估；完成或取消退出当前列表，历史仍可按编号或状态查。小世界长列表统一限高滚动，后台内容定期刷新，探索结果与状态按实际字段呈现。日期显示兼容带时区时间戳。

最终一轮：八套集成105项通过（68秒）；Core 1368通过、17旧问题失败、1跳过。真实 DS 日记回复完成从入库、评估到状态更新的闭环；ARGON medium、OPUS medium 原生探测分别约12秒、6秒。本地日期、模型回退和联系迁移回归均通过。


## DSH 0.2.2-rc.1 复审（2026-11-05，取代上方旧版本计数）

- 完整集成：8 套、106 项通过，70 秒。覆盖独立帮手、轻量子代理、原会话回传、跨会话读取与转告、多人来源归属、日记回复、心愿执行和消费。
- Core：1385 通过、1 跳过；遗留 Node 适配器 10 项通过。恢复公开上游已有的历史方法夹具与运维文档，恢复脱敏导出破坏的 URL/凭据形状；断言未削弱。联系人契约现验证可配置身份和非法身份，而非写死两个联系人。
- 日记/梦境：正文与音频曾共用 `.kin-voice`，音频的固定宽高把正文压坏；音频改用独立 `.kin-audio`。原生页面已核验正文完整显示。
- 后台：失焦/隐藏时暂停动画和页面轮询，重新聚焦立即刷新；前台动效保留。一次对比采样中 GPU/渲染进程由 57.6%/15.9% 降到 0%/0%，不代表所有机器的固定性能。
- DSH：子代理 `list_agents` 已并入控制插件，移除旧子路径挂载；新版模型须在步骤准备前选定，聊天与帮手改在回合开始时选路。真实 DS、OPUS、TERRA 调用通过；订阅插件在新桌面可用。
- 帮手完成事件携带原收件人与会话；渠道“停止任务”同时停止该会话的运行中帮手，不影响其他会话。
- 公开截图来自真实插件与合成演示数据；运行数据、真人聊天与凭据不进入仓库。

安装依赖的 `npm audit` 仍报告 DSH 上游依赖链中的 MCP client、fflate、undici 与 zod 公告。此次没有用强制升级或覆盖版本绕过上游的依赖约束；运行验证通过不能替代上游修复。

## 2026-11-05 补充：发送中断与思考封装

- 健康状态包括已排队/等待回执的输出，模型停止生成不再等同于发送完成。
- 未确认投递在一分钟后先核对，再按原分段 ID 最多重试三次；已确认分段不重发，耗尽重试才系统提醒。测试涵盖途中重启的持久状态、未知回执、恢复后继续剩余分段、飞书 UUID 与微信 client_id。
- 模型入口解析 relay 的显式思考/答案分隔格式，正文流与最终块同时处理；旧会话读取也处理。合成测试遍历每个分片边界，保留普通表情；本机两次实际异常流重放均只留下完整正文。
- 先前 GitHub CI 的 Core、Node、类型和构建已通过；跨会话用例依赖首句一直留在最近 12 条窗口，在慢速环境下不成立。现在在读取前写入测试句，保留原业务断言。

最终本机整体验证：8 套 107 项通过，60 秒。多人归属用例现在等待静默群消息与图谱节点落库后再核对；保留每条消息的说话人、收件人、会话与 actor 断言。输出封装与投递重试回归单独通过。

CI 继续暴露的连发竞态已以延迟入库稳定复现：原定时器在后一条已到达但入库未结束时提前 flush。定时器现等待已接收队列完成，并确认自己仍是最新一只；停止/关闭会使等待中的旧定时器失效。原版回归失败、修复版通过。集成场景把两条连发安排在同一轮事件循环，避免测试本身因 CPU 延迟超出缩短后的合并窗口；正在输入结束的断言等待真实 turn 结束，而非把平台 accepted 当成模型已空闲。


## 评估依据与公开心境修复（2026-11-09）

- Core 全量：1411 通过、1 跳过；保留的两条警告来自上游测试客户端弃用提示，不是业务失败。
- Node 适配器：10 项通过；Node 24 类型检查和构建通过。
- 完整集成：9 套、113 项通过，60 秒；新增 review-health 套件覆盖真实 Mind → Python host 入口、已绑定目的地与健康分类，内部另有 2 个 Core 合成边界检查。
- check-stop、check-runtime、check-session-head 全部通过。
- 新回归覆盖：同源纠正推断、纯主观状态与过期来源、结果/主人参与仍需新证据、聚合记忆全部来源与未展示来源拒绝、结构修复保留正常字段与数组兄弟项、连续两轮评估的公开理由隔离、联系人主评估与各自评估范围、旧摘要告警分类与前台让行统计。
- 精确 reason 恢复工具只处理操作者选定的当前 event/reason，带 revision guard、完整路径核验、正常审计与幂等；历史事件、原回执和数值状态不重写。已被新评估替代的旧诊断不回填。
- 上述自动测试均用隔离合成数据；未为测试向真实联系人发送消息。


CI 首轮发现 contact 合成场景先等主人联系，再确认朋友的未知回执；在慢机器上，这会跨过正常重试时点。场景改为先观察并确认那条未知回执，再等无关联系；原来的同 ID 单次发送断言未放宽，真实传输重试策略未改，同 ID 有界重试仍由渠道和 check-stop 专项覆盖。


## 逐人评估跨联系人目标修复（2026-11-09）

- 先在旧实现验证红灯：逐人评估缺 unified_targets，真实 Mind/Python host 边界断言失败；添加与主评估相同的有效目标清单后转绿。
- 新增朋友原始作用域到主人目标的联系愿望、计划创建、已有计划更新，以及未绑定目标的愿望与计划拒绝。
- 验证原始联系人、对话和队列归属保留，主人的待评估任务未被误消费，仍只有一份共享心智，朋友作用域不取得主人亲密驱动；拒绝后的补评也保留原联系人归属。
- 本次不修改发送策略、人格或历史任务，不用真实联系人发送消息测试。

- 完整验证：Core 1411通过/1跳过；Node 10通过；9套集成113项通过（65秒），其中 review-health 内含5个真实Core边界用例；类型检查、构建与3组专项脚本全过。

## 审查修复发布验证（2026-11-10）

- Core 全量执行一次：1411 通过、4 项因沙箱禁止监听本地端口失败、1 跳过；在允许 loopback 的环境重跑这两个文件，11 项全部通过。最终覆盖 1415 项通过、1 跳过。
- 九套集成首轮 107 项通过；渠道旧断言要求跨会话全局顺序，review-health 的模拟宿主缺少新服务查询入口。更新这两个测试契约后分别重跑，17 项和 5 项通过，九套合计 113 项通过；review-health 内部另有 5 个真实 Core 边界检查。保留同会话精确顺序、总数、唯一性和附件断言。
- Node 适配器 10 项、三组专项脚本 11 项、类型检查和构建通过。
- 使用在线数据库的一致性备份及实际人设、身份作用域、模型路线建立隔离实例；未用合成记忆替换。人设内容和两条路线配置与在线实例一致。隔离实例冻结后台且不接真实渠道。
- 两次现有记忆检索返回记录；原生 DSH 聊天实际执行三次 `kin_recall`，请求含真实人设及回忆上下文，产生可见回复并正常结束。人工检查回复与回忆中的表达偏好相符。没有测试消息发给联系人；原始回复、记忆和凭据仅留在私有验证目录。
- 当前四条不同模型路线均通过实际 API 结构化工具调用；没有修改生产路线或思考档位。这证明本次调用可用，不能替代长时间稳定性、真实平台故障重试或所有人格情境的验证。
- 仍有配置边界：32K 上下文同时预留 16K 输出与 16K 压缩空间时，压缩会被拒绝；需降低输出上限或更换聊天模型。没有为了测试放宽该保护。


## 2026-11-11 continuity and concurrent review repair

- Nine synthetic regression cases fail against the original source and pass against the repair: one shared timer event across recipient orderings; stale operational proposals rejected and freshly judged in both semantic modes; cross-recipient retries; and a context-assembly revision race. Related Core coverage passes 66 tests.
- Full Core suite: 1427 passed, 1 skipped. The first run exposed the pre-existing local SOCKS dependency issue; the clean test-process environment passes without changing product settings or test expectations.
- Compaction regressions use the real DSH transaction and native Session replay with a synthetic model. The original engine accepts a conversational answer and fails the regression; the new engine rejects it before surface replacement. Valid cumulative output, unchanged original events and tail, truncation/empty/missing sections, source identities and media are covered.
- TypeScript typecheck and production build pass. New Python regression file passes Ruff. The five affected existing Python modules retain the same 22 baseline lint findings; no new lint findings were introduced.
- End-to-end suites use isolated local data and fake transports; no real contact messages are sent. Existing parallel test fixtures can race on their non-atomic JSON reads; affected suites are rerun individually with their original assertions.
- Automated checkpoint validation proves structural completeness and preserves transaction boundaries, not semantic perfection of future model-written summaries. No natural production conversation or future model compaction is fabricated as a test.


## 2026-11-11：完整请求预算与错误恢复

- Core 全量：1427 通过、1 跳过；Node 业务测试全量通过。全部离线集成套件运行一次后，按变更重跑模型、压缩、聊天、设置相关套件；类型检查、构建和停止/运行时/会话头检查通过。
- 新增真实 DSH 代理循环回归：先记录完整请求预检溢出，再压缩旧轮次，最终只调用一次模型；本轮用户原话独立保留。旧保留算法在后置运行时上下文场景下被同一断言捕获。
- 注入“段落已经开始投递，模型随后报告上下文错误”并验证没有重新生成，平台回执最终持久化为 accepted。测试使用隔离假平台，没有向真实联系人发送消息。
- 实际 Gemini Responses 适配器携带完整工具定义，完成工具调用、合成结果回放、第二轮最终回答。原版与候选均通过；先前的 schema 400 未复现，不能据此宣称已找到该错误的唯一根因。
- 对同一合成长输入做流式/非流式与有/无工具矩阵：无工具两种模式均读取约 665k tokens，非流式有工具读取约 680k，六处随机校验点全部保留；当前转发路线流式有工具仅报告 127,997，后四处校验点错误。此约束属于实测转发链路，不能泛化为 Gemini 官方窗口。
- KIN 请求输入预算为 648,000，正常预检触发点 583,200；物理窗口仍须容纳请求输出与安全余量。BPE 是本地预算估计，与供应商计数可能有偏差；648k 和 583,200 均指 KIN 的本地预算，并非供应商精确 token 上限。单独配置有工具的转发输入限制，提前切换可承载的备用路线，不降低整条模型列表的预算。


## 2026-11-11: replacement relay and long-input waiting

- The replacement relay's Responses route did not pass: Claude returned an explicit unsupported conversion error; Gemini emitted a tool call followed by a parser error. Production uses the advertised native Messages and Chat Completions endpoints instead, through the existing DSH adapters.
- Real API tests supplied all 68 generic tool definitions without executing business tools or sending contact messages. Both models completed a tool call, detached synthetic result, and successful second response. Synthetic completed history from the previous Responses protocol also passed through the actual KIN route and both new protocols.
- Fresh random long inputs contained six A markers and six different B markers distributed throughout the request. The first tool call had to quote A only; the second response had to recover B from the original input. Both sets passed in full, without first-call leakage of B. Claude reported about 919k input tokens and Gemini about 679k, including reported cache accounting. This verifies retention for these requests, not every future relay response.
- The requested output caps were 128,000 for Claude and 65,536 for Gemini; actual test outputs were short. These tests establish acceptance of the cap, not generation of an entire maximum-length response.
- Long Gemini prefill exceeded the normal first-token deadline. The new opt-in route setting is tested with tool-schema pressure, short-request deadlines, reasoning progress, cancellation, settings persistence and a real KIN long-request round trip. Codex and DeepSeek do not opt in.
- No proxy, upstream dependency patch, synthetic signature, private history fixture or credential is included in the release. The existing 648k/90% compaction and no-regeneration-after-side-effects protections remain active.

- Release checks: Core 1,427 passed / 1 skipped; Node business 10 passed; 13 integration suites 147 passed. Type checking, build and stop/runtime/session-head checks passed. The new wait regression fails against the previously deployed source and passes with the candidate; the strengthened settings-persistence check also passes.


## 2026-11-12 — 心情札记 JSON 隔离验收记录

以下为用户确认上线前的隔离验收记录。该阶段只在独立副本构建和验证；随后获用户明确授权的上线结果见本节末尾。

普通聊天文字必须是完整 JSON：`{private_note:string,action:"send"|"silent",bubbles:[{text:string}]}`。根对象与每个气泡拒绝额外字段；send 要求非空札记和气泡，silent 要求空气泡。纯工具轮可没有文字，工具及媒体仍沿原生调度链路。每步等待正常终态、全部校验及札记保存成功后，只投影气泡正文；平台原有长度和段落拆分保留。原生 reasoning 独立保留，不作为心情札记保存。

格式错误只在本步尚无任何副作用时反馈同一路线纠正一次，第二次失败才进入原 fallback/终态路径；错误原因保留，拒绝文本不加入公开历史。札记保存失败、取消和已释放输出不会套用格式纠错。札记使用 session/turn/step 稳定编号，Core 已有同编号同内容幂等与冲突检测。

本地严格校验是最终边界，当前 DSH 生成请求仅使用协议提示词；不能把另外进行的原生 API `json_schema` 能力探测写成这个通道已启用服务端 grammar。模型仍可能写出不合规内容，它会被拒绝，不会按普通文字外发。

新增 `e2e-reply-output` 覆盖对象气泡、换行/引号/emoji、晚到错误、截断、取消、保存失败、一次纠错、silent、原生签名对齐、并行工具；`e2e-reply-channel` 经过真实 DSH、Core 和假渠道，检查札记先落库、接受账本、公开流与持久历史以及工具错误恢复。`e2e-tool-arguments` 动态读取全部 27 个 Core 工具 schema，合法调用保留，106 个缺字段/错类型/复杂 union/范围和嵌套约束错误在 dispatch 前被拒绝。测试素材均为合成内容，不调用真实联系人业务工具。

同一新渠道测试的初版 8 项在原版源代码隔离快照中 7 项失败、1 项通过，明确捕获普通分析文字和 JSON 原文外发；候选通过这 8 项，后来追加的札记上下文项也通过，最终为 9 项。原有 query/task/voice、媒体与多轮工具集成断言保留，模拟模型的正常聊天文字改为新封套。原 late-overflow 场景尚未公开时允许压缩恢复，最终仅发送一次；已公开文字/已完成工具后的禁止重试仍单独覆盖。


札记上下文使用独立隐藏 `kin-context` 增量，正文只有简短角色/时间标签与札记原文，不携带 `reply_text`、provider 或完整封套，也不在 source metadata 重复正文。下一 pre-step 只精确读取上一已执行步骤的封套札记及兼容 `kin_note` 工具札记；初次启动不扫描所有历史步骤 RPC。`noteIds` 由原生事件持久保存，重启及压缩后不重新塞入旧包。读取失败发生在新步骤开始之前，不会推进并永久略过待注入札记。`evidence_ids` 转为现有删除传播的 `rests`；原话不为省 token 而缩写。

`e2e-reply-context` 验证 1000 个旧步骤仍仅查询上一组 id、原文只注入一次、会话序列化/恢复、压缩替换后去重、读取失败恢复及来源删除。真实 DSH/Core/fake channel 验证下一轮与工具续轮请求中确有上一札记，每个 id 仅一个上下文块。Core bridge 的 schema 校验位于 MCP 归一化之前：额外 135 个错误根值被拦截，原 output/projectContent 保留，合法参数不做类型转换。

候选 Core 全量：1427 通过 / 1 跳过；Node business：10 通过；既有 stop/runtime/session-head：12 项通过。真实 API、最终集成数量和仍受上游影响的场景另按最终结果报告；没有对真实联系人执行测试工具或发送消息。


最终完整 KIN 离线集成：17 套、173 项通过、0 失败（126 秒）。随后仅补充摘要对 `role_note` 的说话人/主观性/不确定性说明，并定向复验 compaction；此规则不将札记提升为用户陈述或已证实事实。札记 API 测试仍由隔离真实模型场景验收，未上线。

### 真实 API 最终验收

每个模型使用独立 DSH、Core 数据目录及假微信渠道；输入只含用户授权的近期消息和合成测试文本，不上传完整真实人设或记忆。四个生产路由 `gemini-4-argon`、`claude-opus-5-5`、`gpt-6.1-terra`、`deepseek-flash` 均完成 3 轮：真实消息回复、下一轮札记回填、参数错误后的连续工具自纠错。每模型有 2 次合成工具尝试（一次失败、一次成功），错误通过实际 agent 的 tool/result 回传；随后正常完成 JSON 回复。共 12 轮全部完成，札记在下一请求中仅出现一次，公开文本未包含完整札记或封套。独立重读四份原生会话日志核对上述计数及终态。

另通过实际适配器运行 36 个场景：近期 3 条消息、单工具、多种查询/任务/语音工具调用与合成结果回放、取消、格式损坏反馈、受控截断恢复、参数错误纠正。首次 34/36 通过；Gemini 一次连接失败、一次在受控截断前被上游 content_filter 终止，均零札记保存、零公开输出。只复测这两项后均通过，原失败证据保留。四模型的格式损坏测试均经历真实 API 首次响应、本地注入格式损坏、错误反馈、第二次真实响应恢复；不将故障注入称为自然复现。

完整 agent 的较早共享测试库曾造成跨模型合成记忆污染，已用上述独立目录验收替代；此前 Gemini 有一次约 60 秒后取消，记录保留，不能由后续通过推断中转不再超时。独立验收完成后，脚手架的附加状态读取误用了不存在的 GET 路由，导致进程退出码 1；这发生在所有断言和报告落盘之后，已经修正诊断地址，另独立核对原生日志。所有临时凭据副本已清除。

原生结构能力单独探测：Gemini/Codex 接受对象气泡的 schema；Claude 接受基础对象结构，但拒绝本次 schema 的 maxItems，不能直接套用全部本地约束；DeepSeek 当前 Messages 兼容接口对无效 format schema 仍返回 200，未证明服务端严格执行。当前候选统一依赖本地校验与纠错，未声称四条路线都开启服务端严格 JSON grammar。实际最大输出截断只在 Gemini/DeepSeek 观察到；Claude/Codex 的小输出预算未产生相同终态，因此它们的此项依据受控截断与离线边界测试，不能宣称已自然复现。

上述结构边界保护 private_note 字段与原始 JSON；它不是对合法 bubbles 内文字语义的证明。札记仍是第一人称主观情绪、碎碎念，不采集供应商隐藏推理；实际样本风格留给用户验收。历史日志能确认普通 text 流绕过隔离并被渠道接受，但没有历史原始 HTTP 报文，不能断言内容最初来自模型隐藏推理、上游转换还是普通分析文字。

验收结束时，候选未提交、未推送、未部署，GitHub CI 未触发；当时生产源码为 dc21ba5 且工作树干净。用户确认后才可同步已验收源码和依赖、在保留当前数据/人设/路由配置的前提下构建重启并复核，再同步 GitHub；不得用测试目录覆盖生产配置。

### 逐模型验收明细

配置中共有四个不同模型。chat 顺序为 Gemini、Claude、Codex、DeepSeek；task 顺序为 DeepSeek、Codex，没有额外未测试的模型 ID。以下为 chat 实际适配器测试；task 后台完整执行采用离线回归与合成工具，不应写成对真人执行了任务或语音。

| 实际 provider/model | 配置 effort | 真实 API 已测 | 完整 agent 的独立环境三轮 |
|---|---|---|---|
| kin-tree-gemini/gemini-4-argon | medium | 是 | 通过 |
| kin-tree-claude/claude-opus-5-5 | medium | 是 | 通过 |
| openai-codex/gpt-6.1-terra | max | 是 | 通过 |
| deepseek-official/deepseek-flash | max | 是 | 通过 |

近期原话 S1/S2/S3 的私密 fixture 编号为 1144/1156/1166；真实原文留在版本库外，不复制进本报告。

| 实际适配器场景 | Gemini | Claude | Codex | DeepSeek |
|---|---|---|---|---|
| 近期原话 S1 | 首次连接失败；单独复测通过 | 通过 | 通过 | 通过 |
| 近期原话 S2 | 通过 | 通过 | 通过 | 通过 |
| 近期原话 S3 | 通过 | 通过 | 通过 | 通过 |
| read_event_thread 调用、合成结果、最终回复 | 通过 | 通过 | 通过 | 通过 |
| read_event_thread + kin_work + kin_voice 调用/合成结果回放 | 通过 | 通过 | 通过 | 通过 |
| 取消后不保存札记、不公开内容 | 通过 | 通过 | 通过 | 通过 |
| 格式损坏反馈后同模型纠正 | 通过 | 通过 | 通过 | 通过 |
| 受控截断后反馈纠正 | 首次被上游过滤；单独复测通过 | 通过 | 通过 | 通过 |
| 参数校验错误、纠正调用、最终回复 | 通过 | 通过 | 通过 | 通过 |

34/36 的两个失败各只做过一次针对性复测：S1 初次 1 次 API 调用在 5.338 秒以 TRANSPORT/Connection error 结束，复测 1 次 API 调用在 22.887 秒正常 stop，保存 1 札记、公开 109 字符。受控截断场景初次 1 次 API 调用在 43.095 秒以 PI_AI_ERROR/content_filter 结束，尚无正文，故未执行截断注入或纠错；复测包含 2 次 API 调用（第一次截断、第二次反馈纠正），合计 15.542 秒，保存 1 札记、公开 78 字符。合计两场景最初 2 次 API 调用，加针对性复测 3 次 API 调用。不能把第一次过滤计为“截断保护失败”，也不能把故意截断后的第二次生成计为额外人工复测。

接收到发送的验证边界：真实/脚本化模型流 → 完整终态缓冲 → JSON 根对象及每个对象气泡校验 → Core 私密札记保存确认 → 仅 bubbles 正文投影到公开流/持久历史 → 假平台 accepted 回执与账本。格式不符、未完成、晚到错误、保存失败均在释放文字/工具前拦截；实际连续工具测试的错误结果由 DSH 自动回给 agent，而不是人工拼接替代整个调度循环。真实平台业务消息未发送。

版本证据：a3cb188（2026-11-05）把私密札记实现为可选 kin_note 工具，描述仍是感受、猜测、留给自己的话；e331968（2026-11-10）加入普通文字的段落流式发送。未找到把札记语义直接改成供应商隐藏推理的变更。扫描快照中的 42 份 native session 未出现 kin_note 调用；近期三例泄露出现在普通 text 块，最近微信案例的渠道账本仍可核验两段 accepted。更准确的故障结论是：札记改为可选工具后未被调用，同时普通分析样文字未受到 JSON 公私隔离；由于缺少历史原始 HTTP wire，分析样文字的最初生成/转换来源尚不确定。


### 用户确认后的上线记录

用户确认逐模型结果后明确授权上线，随后授权同步 GitHub。2026-11-12 已按文件清单替换已验证源码和构建产物，并核对一致性。部署前排空、备份数据库及原产物，SQLite 备份 quick_check 为 ok；停止写入期间 mind_state revision 保持不变。人设、联系人/渠道设置、模型路线顺序、供应商配置和密钥未由部署改写。后台恢复原冻结状态。

重启后的新一轮核心自检无 error；微信完成首轮成功拉取，飞书连接正常。仍有部署前已有的 digests-evidence 警告，保留并单独报告，没有为上线清除诊断。上线验证不发送联系人测试消息；渠道连接不等同于联系人实际收到测试消息。生产模型测试入口对四条实际路线的真实 API 短句检查均成功且正常结束。


### CI 完成边界回归

首轮公开仓库 CI 的集成检查为 170 通过、1 失败：steward 中 Core 已提交 completed，但宿主仍在等待 plan-result 返回，页面 creation.current 仍运行、last 尚为空。测试原来在 Core 完成后立即读取页面，抢在异步宿主发布状态之前。生产业务源码不作修改；测试改为有界等待真实 HTTP 页面上的完成状态，原 13 项最终断言全部保留。

通过仅在测试中延迟 plan-result 返回 5 秒，旧等待逻辑稳定复现 12 通过/页面项 1 失败；新等待逻辑在候选和公开副本相同延迟下各 13 项全部通过；后续 CI 再复核完整公开检查。公开仓库与生产原有基线存在少量差异，本次仅同步授权修复增量，不将两者的全套测试数量混写。
