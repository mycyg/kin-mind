# dsh-kin

KIN 的 DeepSeek Harness 插件：一个常驻的陪伴角色，和普通编码会话并存。

- **KIN 预设**：在 DSH 的对话里选 “KIN” 就是在和她聊。人格来自 `<dataDir>/persona.md`；每轮回答前，她会先想起此刻的心情和相关记忆；聊过的话会写进她的心智。编码会话完全不受影响。
- **KIN 页面**：侧栏点 “KIN”。左边是她自己的聊天框（支持 Markdown 富文本、波形语音条、可折叠工具步骤、悬停时间与免提双向语音陪伴），顶栏可看跨端私聊近况；右边用底部的玻璃 Dock 切换：此刻（互动式心情刻度与「和她说说」、她刚想到的、活跃与缓和心事、心愿、主动联系、心跳）、小世界（梦、可重读已拆时光信的信箱、日记〔可回她〕、心愿与计划〔含「她的手作匣」及文件内嵌预览〕、出门探索〔发现支持「和她聊聊」〕、回忆、日历）、四分栏设置（人格与自动/手动合并精简「她眼中的自己」、两条模型线〔聊天 / 做事，拖动排序、测一下、刷新目录〕与帮手权限、跨端通知 / 微信 / 飞书与 DeepSeek 云端或本地语音、看护自检与冻结）。窄屏时变成单栏。
- **心智**：`../core` 里的 Python 心智，数据在 `<dataDir>/db`。核心请求分成双常驻管道：`fastPipe` 专跑前台记忆检索与页面轻量只读投影（`page` / `settings`），与后台调度互不阻塞；会调模型的活各起一个进程；另有一个常驻的记忆工作者。
- **心跳**：她每分钟看一眼心里有没有要想的事，下次什么时候想由她自己定。你们聊着时她让你先，聊完再静下来想。评估、日记、梦、记忆整理及定期自我认知整理都走做事线，用她的人设；出门探索（Wander）结束后会立刻唤醒评估并在想分享时第一时间触发主动联系，不用再白等一分钟。
- **大总管**：不需要动某个文件夹的联网查资料、汇总类多步任务，用 `kin_background` 转入隔离的 `kin/task` 后台，前台一句快回单步收尾，做完自动带回结果接话；要在你的文件夹里改写项目或文件，仍用 `kin_delegate` 开独立 DSH 工单会话，可用 `kin_work` 查进度、捎话或叫停；顺手小活交给子代理。帮手的权限在设置里选：全权（默认）/ 问我 / 自动审核（要先在 DSH 的插件里打开 Auto review）。
- **认识的人与主动联系**：设置里填每个人在微信 / 飞书上的账号、她能不能主动找；微信/飞书 `3 秒`连发合一再回；主动联系的规矩可配安静时段、每人每天上限与间隔。“此刻”能看到她为什么没找谁、最近发了什么。渠道里发“停止任务 / 取消当前任务 / /停”能叫停她。微信群暂时认不出 @ 她，只能靠插话。
- **渠道**：设置里连微信（扫码）、飞书（App ID / Secret 和地址），她的声音也在那里配（支持 **DeepSeek 云端极速 TTS / ASR** 与本地语音，含音色选择与试听）；账号只存在 `<dataDir>/channels.json`（只有你能读）。
- **记得准**：她想起的每件事都写明是谁、对谁、在哪说的，带着说话那天的日期、星期和时间；“回忆”页能看到这些，记错了点“记错了”写一句，她以后想起都带着这句更正。她也能自己去看别的对话、往别处带话，说不说、说多少由她判断。
- **她的状况**：设置里看她哪里不对、从什么时候起、怎么办（每 5 分钟看一眼、每 4 小时自检、每天 6:40 / 18:40 检查），也能冻结她的后台。

## 装进 DeepSeek Harness 桌面版

1. 准备心智环境（一次）：

   ```bash
   cd ../core && uv sync --python 3.13 --frozen --extra dev --extra vector --extra graph --extra local-embedding
   ```

2. 构建插件（需要 Node 24；桌面版自带的也可以：`/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin`）：

   ```bash
   npm install && npm run build
   ```

3. 退出桌面版，然后安装（也可以在桌面版的 Plugins 页面里选这个文件夹添加）：

   ```bash
   "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" plugin --profile desktop add "$PWD"
   ```

4. 重新打开桌面版。平时的设置都在 KIN 页面里；Plugins 里只放启动用的路径：
   - `dataDir`：心智数据目录，默认 `~/.kin`（第一次启动会初始化一个全新的心智，开关和旧 KIN 一样；已有的心智保留它自己的开关）；
   - `python` / `coreSource`：默认指向本仓库的 `core/.venv` 和 `core/src`；
   - `tokenizerCache`：放有 tiktoken `cl100k_base` 的目录。不填时按字节估算 token，回忆会偏少。

   模型线里能选的，是你在 DSH 设置里已经配好的模型；密钥只在 DSH 里。还没排路线时，两条线都用 DSH 的默认模型。

## 从旧 KIN 搬过来

先退出 DSH，停掉旧 KIN 的所有服务。在 `kin/` 里先预演，看过计划没问题再加 `--apply`：

```bash
node scripts/migrate.mjs --from <放着旧 host/、feishu/ 的目录> --to ~/.kin
```

```bash
node scripts/migrate.mjs --from <放着旧 host/、feishu/ 的目录> --to ~/.kin --apply
```

旧目录只读不改，也不读钥匙串；新目录里会被替换的先挪进 `before-migrate-<时间>/`；可以重跑。搬完在 KIN → 设置里：飞书重填 App Secret 和地址、微信填地址后重新扫码、排好两条模型线、看一遍人设。脚本已在真实旧库和隔离测试库上验证，迁移前仍先核对预演的作用域与目录。

## 开发

```bash
npm run typecheck
npm run build
npm run e2e:all   # 先构建，再并行跑全部离线套件（真实无头 DSH + 脚本化模型 + 真实心智，合成数据），打表；一两分钟
npm run e2e       # 单跑一套：e2e / e2e:talk / e2e:steward / e2e:contact / e2e:channels / e2e:health / e2e:memory / e2e:world
npm run dev    # 起一个隔离的开发版 DSH 网页（独立 DSH_HOME，数据在 ../.dev/serve），打印登录地址
```

测试里宿主的各种等待按开发用的 `pace`（默认 0.05，`KIN_PACE` 可改）缩短；正式用时是 1，什么都不变。记录见 [`docs/VERIFICATION.md`](../docs/VERIFICATION.md)。

开发实例只用 `../.dev/` 下的目录，不会读写你的 `~/.dsh`。macOS 上请用官方 Node 24 跑 `e2e` / `dev`，不要用桌面版自带的 Node：它的签名不允许加载 DSH 依赖里的原生模块。

迁移旧库时，若主人编号不是 `person:person_a`，加 `--owner-id <旧编号>`，联系人和数据库里的身份引用会一起转换。语音转写（ASR）与合成（TTS）优先使用配置的 **DeepSeek 云端接口**（`/v1/tools/asr`、`/v1/tools/tts`，经 `ffmpeg` 转码）以获得秒级响应，也保留本地 `core/scripts/transcribe.py` 与 MLX 语音脚本作离线回退。

Python 的 `local-embedding` 与 `media`/`all` 附加包互斥，不使用 `--all-extras`。

迁移同时恢复旧聊天地址、最后联系时间与近期已接受的主动联系记录，不重播旧消息。计划默认只读未完成的 `active` / `paused`；完成或取消后保留历史，但退出常规上下文和当前列表。按编号或明确状态仍可查询历史。

### Conversation checkpoints

The KIN presets use `dsh-kin/compaction`, a `BasicCompactionEngine` subclass. Active sessions keep the full `648k` input window (compacting at `90%` / `583.2k` to protect prompt cache hits), while sessions idle for 1 hour above `256k` tokens automatically run a background checkpoint compaction on `kin/task` (pruning old tool results first and retaining `20k` recent tokens). The subclass isolates the summarization task from the companion persona, presents earlier records as data without executable tools, and requires all eight non-empty checkpoint sections before allowing replacement. Invalid, empty, truncated or tool-producing output leaves the existing surface intact. Prior checkpoints and later corrections are summarized together; source identities remain attached so the existing deletion filter still applies. Structural validation does not prove semantic completeness of model-written summaries.

`node dev/e2e-compaction.mjs` checks real compaction transactions with a synthetic model, cumulative context, unchanged retained messages, rejection, 1-hour idle >256k background triggers and serialized session replay. `KIN_COMPACTION_BASELINE=1` runs the same assertions against the unmodified upstream engine and exposes the original acceptance bug. No contacts or real sessions are used.


### Long requests on relay routes

A route may set `longInputWaitMs` between `60000` and `300000`. It applies only when the complete BPE request estimate, including tool definitions and results, reaches 128,000 tokens. Smaller requests retain the existing 30-second chat fallback or 60-second deadline. Genuine reasoning or output ends the first-token wait; user cancellation still aborts the request. This does not change the input budget, output reservation, or retry rules after visible output or completed tool calls.

Configure each provider with a protocol its endpoint actually supports. A relay's model list does not prove Responses compatibility. Separate providers can use `anthropic-messages` and `openai-completions`; keep credentials in the DSH credentials service through `apiKeyEnv`. A protocol migration preserves durable history and projects completed old-protocol exchanges only at the request boundary.
