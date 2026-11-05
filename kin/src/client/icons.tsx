/** KIN's small line icons: one stroke weight, round ends, drawn on a 24 grid. */
import type { ReactNode } from 'react'

function Icon({ size = 16, children, filled }: { size?: number; children: ReactNode; filled?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="kin-icon"
      fill={filled ? 'currentColor' : 'none'} stroke={filled ? 'none' : 'currentColor'}
      strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      {children}
    </svg>
  )
}

type P = { size?: number }
export const Sparkle = (p: P) => <Icon {...p} filled><path d="M12 2.8l1.9 5.9 5.9 1.9-5.9 1.9L12 18.4l-1.9-5.9-5.9-1.9 5.9-1.9z" /></Icon>
export const Heart = (p: P) => <Icon {...p} filled><path d="M12 20.3s-7.6-4.6-7.6-10.3A4.3 4.3 0 0 1 12 7.3a4.3 4.3 0 0 1 7.6 2.7c0 5.7-7.6 10.3-7.6 10.3z" /></Icon>
export const Stop = (p: P) => <Icon {...p} filled><rect x="7" y="7" width="10" height="10" rx="2.5" /></Icon>
export const Moon = (p: P) => <Icon {...p}><path d="M19.5 14.6A7.8 7.8 0 1 1 9.4 4.5a6.2 6.2 0 0 0 10.1 10.1z" /></Icon>
export const Mail = (p: P) => <Icon {...p}><rect x="3.5" y="5.5" width="17" height="13" rx="2.5" /><path d="M4.5 7l7.5 5.5L19.5 7" /></Icon>
export const Sliders = (p: P) => <Icon {...p}><path d="M4 7h9M17 7h3M4 17h3M11 17h9" /><circle cx="15" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></Icon>
export const Back = (p: P) => <Icon {...p}><path d="M14.5 5.5L8 12l6.5 6.5" /></Icon>
export const Close = (p: P) => <Icon {...p}><path d="M7 7l10 10M17 7L7 17" /></Icon>
export const Search = (p: P) => <Icon {...p}><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4.2-4.2" /></Icon>
export const Helper = (p: P) => <Icon {...p}><circle cx="9" cy="8.5" r="3" /><path d="M3.5 19a5.5 5.5 0 0 1 11 0" /><path d="M15.5 5.6a3 3 0 0 1 0 5.8M20.5 19a5.5 5.5 0 0 0-3.6-5.2" /></Icon>
export const Bubble = (p: P) => <Icon {...p}><path d="M5 7a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-5l-4.2 3.4V16H8a3 3 0 0 1-3-3z" /></Icon>
export const Pulse = (p: P) => <Icon {...p}><path d="M3 12.5h3.8l2.2-5.2 4.2 10 2.4-4.8H21" /></Icon>
export const Bento = (p: P) => (
  <Icon {...p}><rect x="4" y="4" width="7" height="9.5" rx="2.2" /><rect x="13" y="4" width="7" height="5.5" rx="2.2" /><rect x="13" y="11.5" width="7" height="8.5" rx="2.2" /><rect x="4" y="15.5" width="7" height="4.5" rx="2.2" /></Icon>
)
export const Waves = (p: P) => <Icon {...p}><path d="M4 8.5c2.7-2 5.3 2 8 0s5.3 2 8 0M4 12.5c2.7-2 5.3 2 8 0s5.3 2 8 0M4 16.5c2.7-2 5.3 2 8 0s5.3 2 8 0" /></Icon>
export const Grip = (p: P) => (
  <Icon {...p} filled>
    {[8, 12, 16].flatMap((y) => [9, 15].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.3" />))}
  </Icon>
)
