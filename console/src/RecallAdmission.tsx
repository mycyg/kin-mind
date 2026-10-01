import React, { useCallback, useEffect, useState } from "react";
import { EyeOff, RefreshCw } from "lucide-react";
import { api, commandId, stamp, type Scope } from "./api";

// What the automatic context took in, build by build: the titles of what relevance admission let
// through, and what it left out, counted by reason (kin_mind.recall_admission). Read only, apart
// from the owner's own "不主动提起" toggle on an item.
const settings: Record<string, string> = { off: "关闭", shadow: "影子记录", on: "开启" };
const purposes: Record<string, string> = { chat: "聊天", proactive: "主动联系" };
const states: Record<string, string> = {
  scored: "已打分",
  ranked: "深度排序",
  unavailable: "无法打分，未带入",
  no_query: "没有问题可比",
};
const reasons: Record<string, string> = {
  below_threshold: "相关度不足",
  no_score: "无法打分",
  no_query: "没有问题可比",
  quota: "超出名额",
  seen: "本窗口已见",
  covered: "已被事件摘要覆盖",
  quiet: "不主动提起",
  pool: "排名靠后未评",
  not_selected: "排序未选",
};

type Admitted = { id: string; title: string | null; quiet: boolean };
type Build = {
  at: string;
  purpose: string;
  mode: string;
  setting: string;
  state: string;
  reason: string | null;
  candidates: number;
  admitted: Admitted[];
  exempt: number;
  dropped: Record<string, number>;
  score_ms: number | null;
};

export function RecallAdmissionView({ scope, run, notice }: { scope: Scope; run: any; notice: any }) {
  const [data, setData] = useState<{
    items: Build[];
    setting: string;
    quiet_enabled: boolean;
    quiet: { id: string; title: string | null }[];
  } | null>(null);
  const load = useCallback(
    () => run(async () => setData((await api.call("read_recall_admissions", { query: { ...scope, limit: 30 } })) as any)),
    [scope, run],
  );
  useEffect(() => {
    void load();
  }, [load]);
  const toggle = (id: string, quiet: boolean) =>
    run(async () => {
      await api.call("change_recall_quiet", {
        body: { scope, request: { command_id: commandId(), item_id: id, quiet } } as any,
      });
      notice(quiet ? "已设为不主动提起" : "已恢复自动带入");
      await load();
    });
  if (!data) return null;
  return (
    <>
      <section className="panel admission-panel">
        <div className="panel-title">
          <div>
            <h2>自动带入的记忆</h2>
            <p>
              相关性筛选：{settings[data.setting] ?? data.setting} · 不主动提起：
              {data.quiet_enabled ? "已启用" : "未启用"}
            </p>
          </div>
          <button className="subtle" onClick={() => void load()}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
        {data.items.length ? (
          data.items.map((build, index) => (
            <div className="admission-row" key={build.at + index}>
              <div className="admission-meta">
                <strong>{stamp(build.at)}</strong>
                <small>
                  {purposes[build.purpose] ?? build.purpose} · {build.mode === "deep" ? "深度" : "轻量"} ·{" "}
                  {settings[build.setting] ?? build.setting} · {states[build.state] ?? build.state}
                  {build.score_ms != null ? ` · ${Math.round(build.score_ms)} ms` : ""}
                </small>
              </div>
              <ul className="admitted">
                {build.admitted.length ? (
                  build.admitted.map((item) => (
                    <li key={item.id}>
                      <span title={item.id}>{item.title ?? "（已删除或没有标题）"}</span>
                      <button
                        className="subtle"
                        disabled={!data.quiet_enabled}
                        onClick={() => void toggle(item.id, !item.quiet)}
                      >
                        <EyeOff size={14} />
                        {item.quiet ? "取消不提" : "不主动提起"}
                      </button>
                    </li>
                  ))
                ) : (
                  <li className="muted">没有带入回忆条目</li>
                )}
              </ul>
              <p className="dropped">
                候选 {build.candidates}
                {build.exempt ? ` · 固定带入 ${build.exempt}` : ""}
                {Object.entries(build.dropped).map(([reason, count]) => ` · ${reasons[reason] ?? reason} ${count}`)}
              </p>
            </div>
          ))
        ) : (
          <div className="empty">
            <strong>还没有记录</strong>
            <p>相关性筛选设为影子记录或开启后，每次自动带入都会在这里留下一行。</p>
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>不主动提起</h2>
            <p>这些条目不会被自动带入；问起或主动查询时仍然可以读取。</p>
          </div>
        </div>
        {data.quiet.length ? (
          <ul className="admitted quiet-list">
            {data.quiet.map((item) => (
              <li key={item.id}>
                <span title={item.id}>{item.title ?? "（没有标题）"}</span>
                <button className="subtle" disabled={!data.quiet_enabled} onClick={() => void toggle(item.id, false)}>
                  恢复自动带入
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">没有标记的条目。</p>
        )}
      </section>
    </>
  );
}
