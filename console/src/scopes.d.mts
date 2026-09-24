import type { Scope } from "./api";

export interface ScopeList {
  items: Scope[];
  cursor: string | null;
}

export function listScopes(
  call: (query: { limit: number; cursor?: string }) => Promise<{ items?: Scope[]; cursor?: string | null }>,
  options?: { cursor?: string | null; items?: Scope[]; pages?: number; limit?: number },
): Promise<ScopeList>;
