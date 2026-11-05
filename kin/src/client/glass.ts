/** How her page moves: glass that catches the light under the pointer, and views that glide into one another. */
import { useEffect, useLayoutEffect, useState, type RefObject } from 'react'
import { flushSync } from 'react-dom'

/** Every glass surface under `root` lights up where the pointer is (`--kin-px`, `--kin-py`). */
export function useSheen(root: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const element = root.current
    if (!element) return
    let frame = 0
    const move = (event: PointerEvent) => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const glass = (event.target as Element | null)?.closest?.<HTMLElement>('.kin-glass, .kin-card, .kin-tile')
        if (!glass) return
        const box = glass.getBoundingClientRect()
        glass.style.setProperty('--kin-px', `${event.clientX - box.left}px`)
        glass.style.setProperty('--kin-py', `${event.clientY - box.top}px`)
      })
    }
    element.addEventListener('pointermove', move, { passive: true })
    return () => {
      element.removeEventListener('pointermove', move)
      cancelAnimationFrame(frame)
    }
  }, [root])
}

type Transitioning = Document & { startViewTransition?: (update: () => void) => unknown }

/** A change of view that morphs where the browser can (named elements glide, the rest crossfades) and simply happens elsewhere. */
export function glide(change: () => void) {
  const start = (document as Transitioning).startViewTransition
  if (!start || matchMedia('(prefers-reduced-motion: reduce)').matches) return change()
  start.call(document, () => flushSync(change))
}

/** Whether `root` is narrower than `limit` pixels, kept current as the pane is resized. */
export function useNarrow(root: RefObject<HTMLElement | null>, limit: number) {
  const [narrow, setNarrow] = useState(false)
  useLayoutEffect(() => {
    const element = root.current
    if (!element) return
    // Before the first paint, so the page never shows the wrong layout for a frame.
    setNarrow(element.clientWidth < limit)
    const observer = new ResizeObserver(([entry]) => setNarrow(entry!.contentRect.width < limit))
    observer.observe(element)
    return () => observer.disconnect()
  }, [root, limit])
  return narrow
}


/** Leave the sky still while the app is hidden or another window has focus. */
export function useWindowActive() {
  const active = () => !document.hidden && document.hasFocus()
  const [visible, setVisible] = useState(active)
  useEffect(() => {
    const update = () => setVisible(active())
    document.addEventListener('visibilitychange', update)
    window.addEventListener('focus', update)
    window.addEventListener('blur', update)
    update()
    return () => {
      document.removeEventListener('visibilitychange', update)
      window.removeEventListener('focus', update)
      window.removeEventListener('blur', update)
    }
  }, [])
  return visible
}
