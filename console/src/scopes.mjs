// The scopes the service lists, a page at a time, followed through its cursor (CR-MEM-13). The
// console's picker used to keep the first page and drop the cursor, so the 101st scope and every
// one after it could not be chosen. Plain JavaScript, so the node tests read the same code.

/**
 * Every page from `cursor` on, up to `pages` of them; `call(query)` answers one page of
 * `list_scopes`. Returns what was read, appended to `items`, and the cursor still to follow
 * (null when the list is complete).
 */
export async function listScopes(call, { cursor = null, items = [], pages = 20, limit = 200 } = {}) {
  const found = [...items];
  let next = cursor;
  for (let page = 0; page < pages; page++) {
    const answer = await call({ limit, ...(next ? { cursor: next } : {}) });
    found.push(...(answer?.items ?? []));
    next = answer?.cursor ?? null;
    if (!next) break;
  }
  return { items: found, cursor: next };
}
