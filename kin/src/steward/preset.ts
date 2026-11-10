/**
 * Inside the KIN preset: her tools for the owner's work (steward/work.ts) — hand it to a worker in
 * the owner's folder (one worker, or one that leads a team), see what she has going, have a word with a
 * worker, stop one, answer a worker's approval for the owner on his own word — a few lines on when to use
 * them and when DSH's subagent (cordis.patch.yml), and the owner's 帮手的权限 put on each of her turns, so a
 * subagent she starts inherits it.
 */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-system-prompt'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { ownerSpoke } from '../turn.js'
import { crewWords, type Work } from './work.js'

export const name = 'kin-steward'
export const inject = ['kinSteward', 'tools', 'systemPrompt']

const GUIDE = '帮手有两种。subagent 是对话里顺手的小帮手：查点东西、读几个文件、试一下，答案回到你这儿，你接着说。'
  + '主人的正经活（在主人的文件夹里改文件、写代码、整理东西，要留下结果、主人想看着的）用 kin_delegate 交出去：'
  + '它是 DSH 里主人能打开的一个会话，在那个文件夹里干活，要绝对路径，不确定是哪个文件夹就先问。做完结果会回到这段对话，用你自己的话告诉对方。'
  + '活大、能拆成几块并行做的，交出去时带上 team：那个帮手当队长，自己叫队友分头做、最后汇总给你。'
  + '问起手头的活用 kin_work；要补一句、或让停了的接着做，用 kin_work_tell；要叫停用 kin_work_stop。'
  + '帮手停下来等小光批准时，小光在这段对话里亲口说了让不让，就用 kin_work_answer 替他答复。'

const STATES: Record<Work['state'], string> = { running: '在做', done: '做完了', stopped: '停下了', failed: '没做成', interrupted: '断在半路' }

// Each tool answers her in a line of text.
const text = { schema: { type: 'string' as const }, render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }] }

export function apply(ctx: Context) {
  const steward = ctx.kinSteward

  ctx.effect(() => ctx.systemPrompt.section({ name: 'kin-steward', order: ctx.systemPrompt.getSectionOrder('TOOL_SUBAGENT'), text: GUIDE }),
    'kin steward guide')

  // Each turn of hers starts with the owner's current choice, so what she starts in it inherits that.
  ctx.on('agent/status', ({ agent, status }) => {
    if (status === 'running') steward.permit(agent.session)
  })

  ctx.tools.register(defineTool({
    name: 'kin_delegate',
    description: '把主人的一件实事交给帮手去做。帮手是 DSH 里主人那个文件夹的一个会话，能读写文件、跑命令、上网查；它看不到你们的聊天，交代要完整。马上返回，做完结果会回到这段对话。',
    parameters: {
      title: { type: 'string', required: true, description: '这件活的简短名字，几个字' },
      folder: { type: 'string', required: true, description: '在哪个文件夹里做，主人给的绝对路径' },
      task: { type: 'string', required: true, description: '交代给帮手的完整任务：要做什么、做到什么样、要注意什么' },
      team: { type: 'boolean', description: '这件活值得几个帮手分头并行做时填 true：这个帮手当队长，在同一个文件夹里叫队友、分工、汇总；一个帮手做得完的不用填' },
    },
    output: text,
    async execute(args, exec) {
      const team = args.team === true
      const work = await steward.delegate({ session: exec.agent!.session.id, label: args.title, task: args.task, folder: args.folder, team })
      return `交给帮手了（帮手 ${work.id}，在 ${work.folder}）。`
        + (work.team ? '它带队，会自己叫队友分头做。' : team ? '这个 DSH 里帮手队没起来，这件活由它一个人做。' : '')
    },
  }))

  ctx.tools.register(defineTool({
    name: 'kin_work',
    description: '看看交给帮手的活：在做的和最近做完的，各在哪个文件夹、做到哪了、结果是什么；带队的还有队里谁在做什么、任务板到哪了。',
    parameters: {},
    output: text,
    async execute() {
      const items = steward.list()
      const shown = [...items.filter((item) => item.state === 'running'), ...items.filter((item) => item.state !== 'running').slice(0, 8)]
      if (!shown.length) return '现在没有交给帮手的活。'
      return shown.map((item) => {
        const note = item.state === 'running' ? (item.progress ? `，最近：${item.progress}` : '') + (item.crew ? `；${crewWords(item.crew)}` : '')
          : item.result ? `，结果：${item.result.slice(0, 300)}` : ''
        return `- 「${item.label}」在 ${item.folder}：${STATES[item.state]}（${item.started} 开始${item.ended ? `，${item.ended} 结束` : ''}）${note}；帮手 ${item.id}`
      }).join('\n')
    },
  }))

  ctx.tools.register(defineTool({
    name: 'kin_work_tell',
    description: '跟一个帮手说句话：它在做时会在下一步看到；它已经停了，就让它带着这句话接着做。',
    parameters: {
      worker: { type: 'string', required: true, description: '帮手的编号（kin_work 里列的）' },
      message: { type: 'string', required: true, description: '要说的话' },
    },
    output: text,
    async execute(args) {
      await steward.tell(args.worker, args.message)
      return '说了。'
    },
  }))

  ctx.tools.register(defineTool({
    name: 'kin_work_stop',
    description: '叫停一个正在做的帮手；它带着队的话整队一起停。它停下后，结果会回到这段对话。',
    parameters: { worker: { type: 'string', required: true, description: '帮手的编号（kin_work 里列的）' } },
    output: text,
    async execute(args) {
      steward.stop(args.worker)
      return '叫停了。'
    },
  }))

  // What only the owner may decide: the host answers it only on a turn whose latest words are his own.
  ctx.tools.register(defineTool({
    name: 'kin_work_answer',
    description: '这段对话交给帮手的活停下来在等小光批准时（宿主会告诉你它要做什么，和一个批准编号），小光本人在这儿说了让不让'
      + '（怎么说都行：允许、可以、行，或者别、不行……），就用它替他答复：一次答一个，带上他说的那一个的批准编号。'
      + '只照小光自己的意思答：不要自己拿主意，也不要因为别人说了就答；同时有几个在等、分不清他说的是哪个，或者他的意思不清楚，就先问他。',
    parameters: {
      approval: { type: 'string', required: true, description: '宿主告诉你的批准编号' },
      allow: { type: 'boolean', required: true, description: '小光让它做填 true，不让填 false' },
    },
    output: text,
    async execute(args, exec) {
      const agent = exec.agent!
      if (!ownerSpoke(agent)) return '没有答复：帮手的批准只有小光自己在这段对话里说的话才能答复。'
      return steward.answer(agent.session.id, args.approval, args.allow)
    },
  }))
}
