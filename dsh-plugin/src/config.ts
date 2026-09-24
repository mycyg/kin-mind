/**
 * 插件配置。
 *
 * 召回、上下文预算与整理都在 MemoryPalace 服务里完成，插件只决定发不发、注不注入。
 * 旧的 `.memory/` 文件适配器（`legacyMode`）已随那套存储一起删除；配置里仍写着
 * `legacyMode: true` 时插件拒绝加载（见 `index.ts`），而不是悄悄换成服务传输。
 *
 * @module
 */

import z from '@deepseek-ai/schemastery'

/** 插件配置。 */
export interface Config {
  /** 总开关。关闭后所有监听器立即返回。 */
  enabled: boolean
  /** 项目内护栏日志的目录名：日志写在 `<项目>/<memoryDirName>/log/eventmem-dsh.log`。 */
  memoryDirName: string
  /** 是否在会话启动与压缩之后注入服务返回的工作集。 */
  injectWorkingSet: boolean
  /** 是否把消息、工具结果、todo 快照与回合边界发给服务。 */
  writeFeed: boolean
}

/**
 * 配置 schema。所有字段带默认值，最小可用配置是空对象，因此入参类型是
 * `Partial<Config>`——这与 dsh 仓库内插件惯用的 `z<Config>` 的差别仅在入参侧：
 * 那些插件的字段用 `.required()`，没有默认值可退。
 */
export const Config: z<Partial<Config>, Config> = z.object({
  enabled: z.boolean().default(true).description('总开关'),
  memoryDirName: z.string().default('.memory').description('项目内护栏日志的目录名'),
  injectWorkingSet: z.boolean().default(true).description('会话启动与压缩后注入工作集'),
  writeFeed: z.boolean().default(true).description('把消息、工具结果与回合边界发给服务'),
})
