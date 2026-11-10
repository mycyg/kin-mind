/** Her tools over her calendar besides reading it (the preset's half of calendar.ts, mounted from preset.ts): marking a stretch of days, and routines of her own. */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { daysOf, repeatOf } from './calendar.js'
import { stamp } from './render.js'

export const name = 'kin-calendar-tools'
export const inject = ['kinCalendar', 'tools']

/** Each tool answers her in a few words. */
const answer = { schema: { type: 'string' } as const, render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }] }

export function apply(ctx: Context) {
  const { kinCalendar } = ctx

  ctx.tools.register(defineTool({
    name: 'kin_calendar_mark',
    description: '在你的日程上记下一段日子，或某一天的事：有人跟你说起一段要过的日子（出差、考试周、放假、出去玩），或哪天有件事，你觉得值得放在心上就记。'
      + '记下的会铺在日程上那几天，你和小光都看得见；正过着的那段，每次说话时你都会知道。'
      + '记：写 text 和 from（只有一天就不填 to）。去掉：remove 填它的编号。什么都不填：看看还记着哪些，连同编号。',
    parameters: {
      text: { type: 'string', description: '这段日子是什么，用你自己的话写一句，比如「小光去杭州出差」' },
      from: { type: 'string', description: '从哪天开始，YYYY-MM-DD' },
      to: { type: 'string', description: '到哪天为止（含这天），YYYY-MM-DD；只有一天就不填' },
      remove: { type: 'string', description: '要去掉的那段日子的编号' },
    },
    output: answer,
    async execute({ text, from, to, remove }) {
      if (remove) return kinCalendar.unmark(remove) ? '去掉了。' : '没有这个编号的日子。'
      if (!text?.trim()) {
        const marks = kinCalendar.marks()
        return marks.length
          ? '日程上还记着的日子：\n' + marks.map((mark) => `- ${mark.id}：${daysOf(mark)}「${mark.text}」${mark.by === 'owner' ? '（小光记的）' : ''}`).join('\n')
          : '日程上眼下没有记着的日子。'
      }
      if (!from) throw new Error('写上从哪天开始（YYYY-MM-DD）')
      const mark = kinCalendar.mark({ from, to, text, by: 'kin' })
      return `记下了（编号 ${mark.id}）：${daysOf(mark)}「${mark.text}」。`
    },
  }))

  ctx.tools.register(defineTool({
    name: 'kin_routine',
    description: '给你自己定一件按时重复的事：每天，或每周哪几天的某个时间，你想做的（写点东西、出门看看、想想某个人……）。'
      + '到时候会在你自己的对话里叫你一声，那一轮是你自己的，说的话不会发给任何人，除非你自己去说。'
      + 'add 定一件，list 看看定了哪些，remove 去掉一件。提醒别人的事用 schedule_create。',
    parameters: {
      action: { type: 'string', enum: ['add', 'list', 'remove'], required: true },
      what: { type: 'string', description: 'add：到时候想做的事，用你自己的话写' },
      time: { type: 'string', description: 'add：几点，24 小时制 HH:MM' },
      weekdays: { type: 'array', items: { type: 'integer' }, description: 'add：每周哪几天，1 是周一、7 是周日；不填就是每天' },
      id: { type: 'string', description: 'remove：那件事的编号' },
    },
    output: answer,
    async execute({ action, what, time, weekdays, id }) {
      if (action === 'remove') return id && await kinCalendar.unroutine(id) ? '去掉了。' : '没有这个编号的打算。'
      if (action === 'list') {
        const routines = await kinCalendar.routines()
        return routines.length
          ? '你给自己定的：\n' + routines.map((task) => `- ${task.id}：${task.title}（${repeatOf(task)?.words ?? '重复'}，${task.status === 'active' ? '下一次 ' + stamp(task.scheduledAt) : '停着'}）`).join('\n')
          : '你还没给自己定过。'
      }
      if (!what || !time) throw new Error('写上想做什么和几点')
      const task = await kinCalendar.routine(what, time, weekdays)
      return `定好了（编号 ${task.id}）：${repeatOf(task)?.words} ${time}「${task.title}」，第一次在 ${stamp(task.scheduledAt)}。`
    },
  }))
}
