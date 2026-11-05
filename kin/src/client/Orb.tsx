/** KIN's little light: sakura to lilac, warmer toward peach when her mood is bright, breathing slowly. */
import type { CSSProperties } from 'react'
import { Sparkle } from './icons.js'

export function Orb({ size, warmth }: { size: number; warmth: number }) {
  const style = { '--kin-orb': size + 'px', '--kin-warmth': Math.round(warmth * 100) + '%' } as CSSProperties
  return (
    <span className="kin-orb" style={style} aria-hidden="true">
      <Sparkle size={Math.max(8, Math.round(size * 0.32))} />
    </span>
  )
}

/** 0..1: how bright her overall mood (心情值) is; 0.5 for a mind without one. */
export function warmthOf(dimensions: Record<string, { value: number }> | undefined): number {
  const mood = dimensions?.mood?.value
  return mood === undefined ? 0.5 : Math.max(0, Math.min(1, (mood - 30) / 60))
}
