import React, { useCallback, useEffect, useState } from "react";
import { api, commandId, stamp, type Scope } from "./api";

const kinds: Record<string, string> = { letter: "时光信", diary: "暗房日记" };
// A day in the owner's time zone, the one the service checks the window in (kin_mind.sealed).
const day = (at: number) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Singapore" }).format(at);
function openWindow() {
  const today = day(Date.now());
  const [y, m, d] = today.split("-");
  const last = `${Number(y) + 1}-${m}-${m === "02" && d === "29" ? "28" : d}`;
  return { first: day(Date.now() + 86400000), last };
}

// Letters to Kin and Kin's sealed diaries. Until its day an entry's words are nowhere in the store,
// so this view only ever has its dates and a placeholder; on its day it is an ordinary source.
export function SealedEntries({
  scope,
  run,
  notice,
}: {
  scope: Scope;
  run: any;
  notice: any;
}) {
  const [data, setData] = useState<any>({ items: [], enabled: false });
  const [confirm, setConfirm] = useState("");
  const load = useCallback(async () => {
    setData(await api.call("list_sealed", { query: { ...scope, limit: 50 } }));
  }, [scope]);
  useEffect(() => {
    void run(load);
  }, [run, load]);
  const { first, last } = openWindow();
  return (
    <section className="panel" aria-label="时光信与暗房">
      <div className="panel-title">
        <div>
          <h2>时光信与暗房</h2>
          <p>到日子之前，信和封存的日记谁也读不到，这里只有打开的日期。</p>
        </div>
      </div>
      {data.enabled ? (
        <form
          className="settings-form"
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const values = new FormData(form);
            void run(async () => {
              await api.call("seal_letter", {
                body: {
                  scope,
                  text: String(values.get("text") ?? ""),
                  unlock_at: String(values.get("unlock_at") ?? ""),
                  command_id: commandId(),
                },
              });
              form.reset();
              await load();
              notice("信已封存，到日子才会打开");
            });
          }}
        >
          <label>
            写给 Kin 的信
            <textarea name="text" required maxLength={20000} rows={5} />
          </label>
          <label>
            打开日期
            <input name="unlock_at" type="date" required min={first} max={last} />
          </label>
          <button className="primary">封存</button>
        </form>
      ) : (
        <p className="label-muted">打开记忆设置 sealed_entries 后可以写时光信。</p>
      )}
      {data.items.length === 0 ? (
        <p className="label-muted">还没有封存的信或日记。</p>
      ) : (
        data.items.map((item: any) => (
          <div className="schedule-row sealed-row" key={item.id}>
            <div>
              <strong>{kinds[item.kind] ?? item.kind}</strong>
              <p>
                {item.state === "opened"
                  ? `${item.unlock_at} 已打开，现在是一条普通记录`
                  : item.placeholder}
              </p>
              <p className="label-muted">写于 {stamp(item.created_at)}</p>
            </div>
            {confirm === item.id ? (
              <div className="button-row">
                <button className="subtle" onClick={() => setConfirm("")}>
                  保留
                </button>
                <button
                  className="primary"
                  onClick={() =>
                    void run(async () => {
                      await api.call("erase_sealed", {
                        path: { entry_id: item.id },
                        query: { ...scope },
                      });
                      setConfirm("");
                      await load();
                      notice("已永久删除");
                    })
                  }
                >
                  确认永久删除
                </button>
              </div>
            ) : (
              <button
                className="text-button danger"
                onClick={() => setConfirm(item.id)}
              >
                永久删除…
              </button>
            )}
          </div>
        ))
      )}
    </section>
  );
}
