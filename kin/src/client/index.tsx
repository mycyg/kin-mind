/** Browser half: KIN's sidebar entry and her page (chat, little world, settings). */
import type { Context } from '@deepseek-ai/cordis'
import healthCss from './health.css'
import calendarCss from './calendar.css'
import channelsCss from './channels.css'
import chatCss from './chat.css'
import contactCss from './contact.css'
import css from './kin.css'
import stewardCss from './steward.css'
import talkCss from './talk.css'
import { createPage } from './Page.js'

/** Her stylesheets: the page's own, then one per feature (each feature imports its own .css and adds it here). */
const STYLES = [css, healthCss, channelsCss, talkCss, stewardCss, contactCss, chatCss, calendarCss]

export const name = 'kin-client'
export const inject = ['slots', 'sessions']

export function apply(ctx: Context) {
  ctx.effect(() => {
    const style = document.createElement('style')
    style.dataset.kin = ''
    style.textContent = STYLES.join('\n')
    document.head.append(style)
    return () => style.remove()
  }, 'kin styles')

  const Page = createPage(ctx)
  const slots = (ctx as any).slots

  ctx.effect(() => slots.inject('main', () => slots.register({ name: 'main', key: 'kin' }, Page)), 'kin page')

  ctx.effect(() => slots.inject('sidebar.panellist', () => slots.register({
    name: 'sidebar.panellist', id: 'kin', label: 'KIN', order: 5,
  }, KinIcon)), 'kin sidebar entry')
}

function KinIcon({ size = 16, active }: { size?: number; active?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" data-active={active ? '' : undefined}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.6l1.25 3.15 3.15 1.25-3.15 1.25L12 16.4l-1.25-3.15L7.6 12l3.15-1.25z" fill="currentColor" stroke="none" />
    </svg>
  )
}
