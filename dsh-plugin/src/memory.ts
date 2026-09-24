/**
 * 项目内护栏日志的位置。
 *
 * 记忆本身在 MemoryPalace 服务里；插件在项目下只写一份护栏日志
 * `<项目>/<memoryDirName>/log/eventmem-dsh.log`，便于在出错的项目里就地查看。
 *
 * @module
 */

import { homedir } from 'node:os'
import { join } from 'node:path'

import { relativeToProject, resolveProjectDir } from './relpath.js'

/** 一个项目的护栏日志布局。 */
export class MemoryPaths {
  /** 已解析的项目根目录绝对路径。 */
  readonly projectDir: string
  /** 项目内目录 `<project>/.memory`。 */
  readonly root: string

  private constructor(projectDir: string, memoryDirName: string) {
    this.projectDir = projectDir
    this.root = join(projectDir, memoryDirName)
  }

  /**
   * 由会话工作目录构造路径。
   *
   * @param projectDir - 会话工作目录（`session.header.cwd`）。
   * @param memoryDirName - 目录名，默认 `.memory`。
   * @returns 路径视图。
   */
  static forProject(projectDir: string, memoryDirName = '.memory'): MemoryPaths {
    return new MemoryPaths(resolveProjectDir(projectDir, homedir()), memoryDirName)
  }

  /** 护栏日志目录。 */
  get logDir(): string {
    return join(this.root, 'log')
  }

  /** 本适配器的护栏日志。 */
  get adapterLog(): string {
    return join(this.logDir, 'eventmem-dsh.log')
  }

  /**
   * 把路径规约为项目内的 POSIX 相对路径。
   *
   * @param path - 待规约的路径。
   * @returns 项目内相对路径。
   */
  relative(path: string): string {
    return relativeToProject(this.projectDir, path)
  }
}
