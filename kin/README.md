# dsh-kin

KIN 的 DeepSeek Harness 插件：一个常驻的陪伴角色，和普通编码会话并存。

- **KIN 预设**：在 DSH 的对话里选 “KIN” 就是在和她聊。人格来自 `<dataDir>/persona.md`；每轮回答前，她会先想起此刻的心情和相关记忆；聊过的话会写进她的心智。编码会话完全不受影响。
- **KIN 页面**：侧栏点 “KIN”。左边是她自己的聊天框；右边用底部的玻璃 Dock 切换：此刻（她现在的心情、刚想到的、心事、心愿）、小世界（梦、信、日记、心愿、出门探索、回忆）、设置（改人设、排两条模型线〔聊天 / 做事，拖动排序、测一下〕、帮手的权限、内心开关、她认识的人与主动联系的规矩、微信 / 飞书账号与她的声音、她的状况）。窄屏时变成单栏。
- **心智**：`../core` 里的 Python 心智，数据在 `<dataDir>/db`。短读写在一个常驻进程里；会调模型的活各起一个进程；另有一个常驻的记忆工作者。
- **心跳**：她每分钟看一眼心里有没有要想的事，下次什么时候想由她自己定。你们聊着时她让你先，聊完再静下来想。评估、日记、梦、记忆整理都走做事线，用她的人设；页面“此刻”底下能看到她上次想事情是什么时候。
- **大总管**：你让她做的事（处理文件、写代码、查资料），她用 `kin_delegate` 在你的文件夹里开一个 DSH 会话交给帮手（做事线），盯进度、能叫停，做完在你问她的那段对话里用她的话告诉你，也记进她的经历；顺手的小活交给 DSH 子代理。帮手的权限在设置里选：全权（默认）/ 问我 / 自动审核（要先在 DSH 的插件里打开 Auto review）。她自己也会出门探索、做点东西，帮手只在她自己的目录里动手。
- **认识的人与主动联系**：设置里填每个人在微信 / 飞书上的账号、她能不能主动找；主动联系要守的规矩默认安静时段 22–8 点、每人每天最多 3 次、间隔 1 小时、等回了再找。“此刻”能看到她为什么没找谁、最近发了什么。渠道里发“停止任务 / 取消当前任务 / /停”能叫停她。微信群暂时认不出 @ 她，只能靠插话。
- **渠道**：设置里连微信（扫码）、飞书（App ID / Secret 和地址），她的声音也在那里配；账号只存在 `<dataDir>/channels.json`（只有你能读）。
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

迁移旧库时，若主人编号不是 `person:person_a`，加 `--owner-id <旧编号>`，联系人和数据库里的身份引用会一起转换。语音转写使用 `channels.json` 的 `hearing: { python, model }`，调用 `core/scripts/transcribe.py`；优先保留平台已有的转写。

Python 的 `local-embedding` 与 `media`/`all` 附加包互斥，不使用 `--all-extras`。

迁移同时恢复旧聊天地址、最后联系时间与近期已接受的主动联系记录，不重播旧消息。计划默认只读未完成的 `active` / `paused`；完成或取消后保留历史，但退出常规上下文和当前列表。按编号或明确状态仍可查询历史。

### Conversation checkpoints

The KIN presets use `dsh-kin/compaction`, a `BasicCompactionEngine` subclass. DSH still owns range selection, locks, token pricing, retained messages and append-only checkpoint commits. The subclass isolates the summarization task from the companion persona, presents earlier records as data without executable tools, and requires all eight non-empty checkpoint sections before allowing replacement. Invalid, empty, truncated or tool-producing output leaves the existing surface intact. Prior checkpoints and later corrections are summarized together; source identities remain attached so the existing deletion filter still applies. Structural validation does not prove semantic completeness of model-written summaries.

`node dev/e2e-compaction.mjs` checks real compaction transactions with a synthetic model, cumulative context, unchanged retained messages, rejection and serialized session replay. `KIN_COMPACTION_BASELINE=1` runs the same assertions against the unmodified upstream engine and exposes the original acceptance bug. No contacts or real sessions are used.
