import type { Session } from '@deepseek-ai/dsh-session'

export class ReceiptSurfaceNotReadyError extends Error {
  constructor() {
    super('session system prompt has not been established; receipt write deferred')
    this.name = 'ReceiptSurfaceNotReadyError'
  }
}

/** Receipt notices must not occupy the surface before DSH installs its first system prompt. */
export function assertReceiptSurfaceReady(session: Session): void {
  const first = session.surface.nodes[0]
  if (first === undefined || session.eventAt(first)?.type !== 'system/message') {
    throw new ReceiptSurfaceNotReadyError()
  }
}
