/**
 * The sky behind her page: soft painted light in her colours, warmer toward peach when her mood is
 * bright, drifting slowly. Dawn in DSH's light theme, night with a few stars in its dark one.
 */
import type { CSSProperties } from 'react'

export function Sky({ warmth }: { warmth: number }) {
  return (
    <div className="kin-sky" style={{ '--kin-warmth': Math.round(warmth * 100) + '%' } as CSSProperties} aria-hidden="true">
      <i data-glow="1" />
      <i data-glow="2" />
      <i data-glow="3" />
      <i data-glow="4" />
      <i className="kin-sky-clouds" />
      <i className="kin-sky-stars" />
      <i className="kin-sky-grain" />
    </div>
  )
}
