// Model lanes, the Node half. Node never opens the ledger database: leases are
// taken over an injected transport (the host's memoryRequest, falling back to the
// model-lease CLI action). Every failure of that transport is a degraded mode,
// never an outage of the caller: an unreachable ledger must not stop the owner's
// own work, and it must not silently let background work run unbounded either.

export const LANES = ['foreground', 'user-work', 'background'];
// Foreground is never queued, and a user-work review that never runs would hold
// the work lock it exists to release. Only background yields a run it cannot admit.
export const PROCEEDS_WHILE_DEGRADED = new Set(['foreground', 'user-work']);
// A lost lease means the slot is somebody else's now. Foreground holds no slot,
// so losing one is only recorded.
export const ABORTS_WHEN_LOST = new Set(['user-work', 'background']);
const ANSWERS = new Set(['admitted', 'wait', 'disabled', 'renewed', 'lost', 'released']);
const LABEL = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,119}$/;
const DEFAULT_RETRY_SECONDS = 30;

/** One row per model call, written on every exit of that call. Unknown usage is
 * written as unknown: an absent `usage` key vanishes through JSON.stringify, and
 * a zero would be a lie about what was spent. */
export function usageRow({usage, usageStatus, ...rest}) {
  const known = usage && typeof usage === 'object' && Object.keys(usage).length > 0;
  return {...rest, usage: known ? usage : null, usageStatus: usageStatus ?? (known ? 'reported' : 'unknown')};
}

/** A caller-chosen id makes acquire repeatable after a lost answer, and lets the
 * caller release a lease whose admission it never saw. */
export function leaseId(purpose, random = () => Math.random().toString(36).slice(2)) {
  const label = String(purpose).replace(/[^A-Za-z0-9:._-]/g, '-').slice(0, 40);
  return (label + '-' + random() + random() + '00000000').slice(0, 120);
}

export function createLeaseClient({request, now = () => Date.now(), setTimer = setTimeout,
  clearTimer = clearTimeout, note = () => {}, holder = 'kin-node', ttlSeconds = 90,
  waitBudgetMs = 0, random} = {}) {
  if (typeof request !== 'function') throw Error('lease-transport-required');

  const call = async (op, body) => {
    try {
      const answer = await request('model-leases/' + op, body);
      // A busy or rolled-back ledger, and anything unreadable, is one degraded mode.
      if (!answer || !ANSWERS.has(answer.state)) return {state: 'unavailable', reason: 'lease-service-unreadable'};
      return answer;
    } catch {
      // Never keep a transport message: it can name hosts, paths or credentials.
      return {state: 'unavailable', reason: 'lease-service-unreachable'};
    }
  };

  const pause = (ms, signal) => new Promise(resolve => {
    const timer = setTimer(resolve, ms);
    timer?.unref?.();
    signal?.addEventListener?.('abort', () => {clearTimer(timer); resolve();}, {once: true});
  });

  function hold({lane, purpose, id, ttl, answer}) {
    const state = answer.state === 'admitted' ? 'admitted' : answer.state === 'disabled' ? 'disabled'
      : answer.state === 'wait' ? 'wait' : 'degraded';
    const reason = answer.reason ?? (state === 'degraded' ? 'lease-service-unreadable' : null);
    // Degraded and wait are different refusals: one is an ignorant ledger, the other
    // an informed no. Only foreground overrides an informed no, because it is never queued.
    const proceed = state === 'admitted' || state === 'disabled'
      || (state === 'degraded' && PROCEEDS_WHILE_DEGRADED.has(lane)) || (state === 'wait' && lane === 'foreground');
    const controller = state === 'admitted' && ABORTS_WHEN_LOST.has(lane) ? new AbortController() : null;
    let timer = null, watchdog = null, confirmedUntil = null, lost = false, expired = false, done = state !== 'admitted';

    const schedule = seconds => {
      timer = setTimer(renew, Math.max(1000, (Number(seconds) > 0 ? Number(seconds) : ttl / 3) * 1000));
      timer?.unref?.();
    };
    // The last deadline the ledger confirmed. An unanswered renewal is not a loss — but
    // it is not a renewal either: once this instant passes, the server may hand the slot
    // to somebody else, and a call still running would spend resources nobody is counting.
    const arm = () => {
      if (watchdog !== null) clearTimer(watchdog);
      watchdog = setTimer(expire, Math.max(0, confirmedUntil - now()));
      watchdog?.unref?.();
    };
    const expire = () => {
      watchdog = null;
      if (done || expired) return;
      if (now() < confirmedUntil) {arm(); return;} // the clock moved backwards; keep waiting
      expired = true;
      note({event: 'model-lease-expired-unconfirmed', lane, purpose, id});
      // Foreground holds no controller: there the expiry is only recorded, as with a loss.
      controller?.abort(Error('model-lease-expired-unconfirmed'));
    };
    const confirm = () => {confirmedUntil = now() + ttl * 1000; arm();};
    const renew = async () => {
      timer = null;
      if (done || expired) return;
      const result = await call('renew', {id, ttl_seconds: ttl});
      if (done || expired) return;
      if (result.state === 'lost') {
        lost = true;
        note({event: 'model-lease-lost', lane, purpose, id});
        if (watchdog !== null) {clearTimer(watchdog); watchdog = null;}
        controller?.abort(Error('model-lease-lost'));
        return;
      }
      // An unanswered renewal is not a loss. Only the ledger can say the slot was
      // given away; aborting a call that is already paid for on a transport hiccup
      // would spend the money and keep nothing.
      if (result.state !== 'renewed') note({event: 'model-lease-renewal-unanswered', lane, purpose, id});
      else confirm();
      schedule(result.lease?.renew_after_seconds);
    };
    if (state === 'admitted') {schedule(answer.lease?.renew_after_seconds); confirm();}

    return {
      lane, purpose, id, state, reason, proceed,
      get lost() {return lost;},
      get expired() {return expired;},
      signal: controller?.signal ?? null,
      capacity: answer.capacity ?? null,
      retryAfterSeconds: state === 'wait' ? (Number(answer.retry_after_seconds) || DEFAULT_RETRY_SECONDS) : null,
      detail() {
        return {lane, purpose, leaseState: state, ...(reason ? {leaseReason: reason} : {}), ...(lost ? {leaseLost: true} : {}),
          ...(expired ? {leaseExpiredUnconfirmed: true} : {})};
      },
      async release() {
        if (done) {done = true; return {state};}
        done = true;
        if (timer !== null) {clearTimer(timer); timer = null;}
        if (watchdog !== null) {clearTimer(watchdog); watchdog = null;}
        return call('release', {id});
      },
    };
  }

  /** Never throws for a refusal: the answer is the handle, and `proceed` says what
   * the contract allows this lane to do with it. */
  async function acquire({lane, purpose, holder: who = holder, id, ttlSeconds: ttl = ttlSeconds,
    budgetMs = waitBudgetMs, signal} = {}) {
    if (!LANES.includes(lane)) throw Error('unknown-model-lane');
    if (!LABEL.test(String(purpose))) throw Error('invalid-lease-purpose');
    const key = id ?? leaseId(purpose, random);
    const deadline = now() + budgetMs;
    let answer;
    for (;;) {
      answer = await call('acquire', {lane, purpose, holder: who, id: key, ttl_seconds: ttl});
      if (answer.state !== 'wait') break;
      const retryMs = (Number(answer.retry_after_seconds) > 0 ? Number(answer.retry_after_seconds) : DEFAULT_RETRY_SECONDS) * 1000;
      if (lane === 'foreground' || signal?.aborted || now() + retryMs > deadline) break;
      await pause(retryMs, signal);
    }
    return hold({lane, purpose, id: key, ttl, answer});
  }

  /** Runs `fn(held)` under a lease, releasing it on every exit. A lane that may not
   * run gets a skip, not a silent success: the caller decides how to report it. */
  async function withLease(lane, purpose, fn, options = {}) {
    const held = await acquire({lane, purpose, ...options});
    if (!held.proceed) {
      await held.release();
      throw Object.assign(Error('model-lane-' + held.state), {leaseSkipped: true, lease: held.detail()});
    }
    try {
      return await fn(held);
    } finally {
      await held.release();
    }
  }

  return {acquire, withLease};
}
