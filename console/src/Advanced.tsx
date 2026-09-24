import { useEffect, useState } from "react";
import { api, save, type Scope } from "./api";

export function FamilyEditor({
  families,
  scope,
  run,
  refresh,
}: {
  families: any[];
  scope: Scope;
  run: (fn: () => Promise<any>) => Promise<any>;
  refresh: () => Promise<any>;
}) {
  const [chosen, setChosen] = useState(""),
    [action, setAction] = useState("create");
  const [loaded, setLoaded] = useState<any>(null);
  useEffect(() => {
    let active = true;
    setLoaded(null);
    if (chosen)
      void run(async () => {
        let offset = 0,
          members: string[] = [],
          full: any;
        do {
          const page = await api.call("read_family", {
            path: { family_id: chosen },
            query: { offset, limit: 200 },
          });
          if (full && full.revision !== page.revision)
            throw new Error("成员已更新，请重新选择对象。");
          full = page;
          members.push(...page.members);
          offset = Number(page.cursor || 0);
        } while (offset);
        if (active) setLoaded({ ...full, members });
      });
    return () => {
      active = false;
    };
  }, [chosen, families]);
  const family = loaded?.id === chosen ? loaded : null;
  return (
    <details className="panel">
      <summary>家族、卷与成员管理</summary>
      <form
        className="settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          const members = String(f.get("members") || "")
            .split(/[\s,]+/)
            .filter(Boolean);
          void run(async () => {
            if (action === "create") {
              await api.call("create_family", {
                body: {
                  scope,
                  title: String(f.get("title")),
                  members,
                  kind: f.get("kind") as any,
                },
              });
            } else if (family) {
              await api.call("change_family", {
                path: { family_id: family.id },
                body: {
                  expected_revision: family.revision,
                  action: action as any,
                  members: members.length ? members : undefined,
                  title: String(f.get("title") || "") || undefined,
                  target: String(f.get("target") || "") || undefined,
                  target_revision: Number(f.get("revision")) || undefined,
                },
              });
            }
            await refresh();
          });
        }}
      >
        <div className="form-grid">
          <label>
            操作
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              {[
                ["create", "新建"],
                ["revise", "修改成员"],
                ["merge", "合并"],
                ["split", "拆分"],
                ["rollback", "回滚"],
                ["archive", "归档"],
              ].map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            当前家族 / 卷
            <select
              value={chosen}
              onChange={(e) => setChosen(e.target.value)}
              required={action !== "create"}
            >
              <option value="">选择对象</option>
              {families.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.title} · r{f.revision}
                </option>
              ))}
            </select>
          </label>
          <label>
            名称
            <input name="title" required={action === "create"} />
          </label>
          <label>
            类型
            <select name="kind">
              <option value="family">主题家族</option>
              <option value="volume">叙事卷</option>
            </select>
          </label>
        </div>
        {["create", "revise", "split"].includes(action) && (
          <label>
            成员 id（用逗号或换行分隔）
            <textarea
              key={`${chosen}-${family?.revision}-${action}`}
              name="members"
              rows={4}
              defaultValue={
                action === "revise" ? family?.members.join("\n") : ""
              }
            />
          </label>
        )}
        {action === "merge" && (
          <label>
            合并来源
            <select name="target" required>
              {families
                .filter((f) => f.id !== chosen)
                .map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.title}
                  </option>
                ))}
            </select>
          </label>
        )}
        {action === "rollback" && (
          <label>
            恢复到 revision
            <input
              type="number"
              name="revision"
              min="1"
              max={(family?.revision ?? 2) - 1}
              required
            />
          </label>
        )}
        <button className="primary" disabled={action !== "create" && !family}>
          保存变更
        </button>
      </form>
      <form
        className="settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          void run(async () => {
            await api.call("create_relation", {
              body: {
                subject: String(f.get("subject")),
                object: String(f.get("object")),
                predicate: String(f.get("predicate")),
              },
            });
            await refresh();
          });
        }}
      >
        <h3>添加记录关系</h3>
        <div className="form-grid">
          <label>
            起始记录
            <input name="subject" placeholder="mem_…" required />
          </label>
          <label>
            目标记录
            <input name="object" placeholder="mem_…" required />
          </label>
        </div>
        <label>
          关系
          <select name="predicate">
            {[
              ["related", "相关"],
              ["supports", "支持"],
              ["refutes", "反驳"],
              ["coexists", "并存"],
              ["verifies", "验证"],
              ["counterexample", "反例"],
              ["part_of", "组成部分"],
              ["follows", "后续事件"],
            ].map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <button className="subtle">添加关系</button>
      </form>
    </details>
  );
}

export function AttachmentPreview({
  source,
  locator,
  run,
}: {
  source: any;
  locator: any;
  run: (fn: () => Promise<any>) => Promise<any>;
}) {
  const [url, setUrl] = useState(""),
    [mime, setMime] = useState(""),
    [page, setPage] = useState(locator?.page ?? 1),
    [start, setStart] = useState(locator?.start_seconds ?? 0),
    [text, setText] = useState(""),
    [unsupported, setUnsupported] = useState(false);
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  useEffect(() => {
    setUrl("");
    setText("");
    setUnsupported(false);
    setPage(locator?.page ?? 1);
    setStart(locator?.start_seconds ?? 0);
  }, [source.id]);
  const preview = () =>
    run(async () => {
      let bytes: any,
        type = source.media_type;
      if (type === "application/pdf") {
        bytes = await api.call("read_page", {
          path: { source_id: source.id },
          query: { page },
        });
        type = "image/png";
      } else if (type.startsWith("audio/") || type.startsWith("video/")) {
        bytes = await api.call("read_clip", {
          path: { source_id: source.id },
          query: { start, end: start + 30 },
        });
        type = type.startsWith("video/") ? "video/mp4" : "audio/wav";
      } else {
        if (source.byte_length > 32 * 1024 * 1024)
          throw new Error("附件超过 32 MiB，请下载原文件查看。");
        bytes = await api.call("read_attachment", {
          path: { source_id: source.id },
        });
      }
      setUnsupported(false);
      if (type.startsWith("text/") || /[/+](json|x-ndjson|xml|yaml|csv)$/.test(type)) {
        setText(new TextDecoder().decode(bytes));
        return;
      }
      if (!/^(image|video|audio)\//.test(type)) {
        setUnsupported(true);
        return;
      }
      setMime(type);
      setUrl(URL.createObjectURL(new Blob([bytes], { type })));
    });
  return (
    <section className="attachment-preview">
      <div className="button-row">
        {source.media_type === "application/pdf" && (
          <label>
            页码
            <input
              type="number"
              min="1"
              value={page}
              onChange={(e) => setPage(Number(e.target.value))}
            />
          </label>
        )}
        {/^(audio|video)\//.test(source.media_type) && (
          <label>
            起始时间（秒）
            <input
              type="number"
              min="0"
              value={start}
              onChange={(e) => setStart(Number(e.target.value))}
            />
          </label>
        )}
        <button className="subtle" onClick={() => void preview()}>
          预览附件
        </button>
      </div>
      {url && mime.startsWith("image/") && (
        <img src={url} alt={`${source.title} · 原始附件`} />
      )}{" "}
      {url && mime.startsWith("video/") && <video src={url} controls />}
      {url && mime.startsWith("audio/") && <audio src={url} controls />}
      {text && <pre>{text}</pre>}
      {unsupported && (
        <p className="form-help">这种附件不能在这里预览，请下载原文件查看。</p>
      )}
    </section>
  );
}

export function DataControls({ run, notice }: { run: any; notice: any }) {
  const download = (kind: string) =>
    run(async () => {
      const created = await api.call("create_download", { path: { kind } });
      const place = created.details?.path ? `：${created.details.path}` : "";
      if (created.status !== "ready") {
        notice(`${created.id} 正在生成，完成后在服务的导出目录里${place}`);
        return;
      }
      // Streamed into a Blob by the browser, never held as one ArrayBuffer by the page.
      await save(
        await api.response("download_export", { path: { name: created.id } }),
        created.id,
      );
      notice(`导出文件已生成${place}`);
    });
  return (
    <section className="panel">
      <h2>备份、导出与恢复</h2>
      <p>恢复在独立目录中执行，保留当前记忆库。</p>
      <div className="button-row">
        <button className="subtle" onClick={() => void download("backup")}>
          下载完整备份
        </button>
        <button className="subtle" onClick={() => void download("export")}>
          导出记忆 JSONL
        </button>
      </div>
      <form
        className="settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          const form = new FormData(e.currentTarget);
          void run(async () => {
            const result = await api.call("restore_backup", { form });
            notice(`备份已恢复到独立目录：${result.path}`);
          });
        }}
      >
        <label>
          选择备份文件
          <input type="file" name="file" accept=".gz,.tar" required />
        </label>
        <button className="subtle">校验并恢复到独立目录</button>
      </form>
    </section>
  );
}

const budgetScenarios = ["tool", "companion", "knowledge"],
  budgetPhases = ["startup", "passive", "cumulative"];
const budgetDefault = (s: string, p: string) =>
  p === "startup"
    ? s === "companion"
      ? 4000
      : 2000
    : p === "passive"
      ? s === "companion"
        ? 512
        : 256
      : s === "companion"
        ? 16000
        : 12000;
export function BudgetSettings({ run, notice }: { run: any; notice: any }) {
  const [values, setValues] = useState<any>({});
  const shown = (s: string, p: string) => values[s]?.[p] ?? budgetDefault(s, p);
  useEffect(() => {
    void run(async () =>
      setValues(await api.call("read_settings", { path: { key: "budgets" } })),
    );
  }, [run]);
  return (
    <section className="panel">
      <h2>上下文预算</h2>
      <form
        className="settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          const form = new FormData(e.currentTarget);
          const edited: [string, string, number][] = [];
          for (const s of budgetScenarios)
            for (const p of budgetPhases) {
              const value = Number(form.get(s + "-" + p));
              if (value !== shown(s, p)) edited.push([s, p, value]);
            }
          void run(async () => {
            // Saving replaces the whole key. Scenarios this form does not show, and changes made
            // elsewhere since it was read, are kept by starting from the stored value.
            const next: any = {
              ...(await api.call("read_settings", { path: { key: "budgets" } })),
            };
            for (const [s, p, value] of edited) next[s] = { ...next[s], [p]: value };
            await api.call("configure_settings", {
              path: { key: "budgets" },
              body: next,
            });
            setValues(next);
            notice("上下文预算已保存");
          });
        }}
      >
        {budgetScenarios.map((s) => (
          <div className="budget-grid" key={s}>
            <strong>
              {{ tool: "工具协作", companion: "陪伴", knowledge: "知识" }[s]}
            </strong>
            {budgetPhases.map((p) => (
              <label key={p}>
                {
                  {
                    startup: "启动",
                    passive: "被动召回",
                    cumulative: "累计上限",
                  }[p]
                }
                <input
                  name={s + "-" + p}
                  type="number"
                  min="0"
                  max="128000"
                  key={`${s}-${p}-${values[s]?.[p]}`}
                  defaultValue={shown(s, p)}
                />
              </label>
            ))}
          </div>
        ))}
        <button className="primary">保存预算</button>
      </form>
    </section>
  );
}

export function DeleteControl({
  id,
  run,
  onDeleted,
}: {
  id: string;
  run: any;
  onDeleted: () => void;
}) {
  const [preview, setPreview] = useState<any>(null);
  return (
    <div className="delete-control">
      {!preview ? (
        <button
          className="text-button danger"
          onClick={() =>
            void run(async () =>
              setPreview(
                await api.call("preview_deletion", { path: { object_id: id } }),
              ),
            )
          }
        >
          永久删除…
        </button>
      ) : (
        <div className="delete-preview">
          <p>
            将删除 {preview.record_count} 条记录、{preview.source_count}{" "}
            个原始来源及其修订、附件和派生内容。
          </p>
          <p>已经导出的备份需要单独处理。</p>
          <div className="button-row">
            <button className="subtle" onClick={() => setPreview(null)}>
              保留
            </button>
            <button
              className="primary"
              onClick={() =>
                void run(async () => {
                  await api.call("delete_object", { path: { object_id: id } });
                  onDeleted();
                })
              }
            >
              确认永久删除
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function TimelineCalendar({
  items,
  onRead,
}: {
  items: any[];
  onRead: (id: string) => void;
}) {
  const [month, setMonth] = useState(() =>
    new Date().toISOString().slice(0, 7),
  );
  const year = Number(month.slice(0, 4)),
    m = Number(month.slice(5)),
    days = new Date(year, m, 0).getDate();
  const offset = (new Date(year, m - 1, 1).getDay() + 6) % 7;
  return (
    <section className="panel">
      <div className="panel-title">
        <h2>记忆日历</h2>
        <label>
          月份
          <input
            type="month"
            value={month}
            onChange={(e) => setMonth(e.target.value)}
          />
        </label>
      </div>
      <p>显示当前已加载记录的有效日期。</p>
      <div className="calendar-grid">
        {["一", "二", "三", "四", "五", "六", "日"].map((d) => (
          <strong key={d}>{d}</strong>
        ))}
        {Array.from({ length: offset }, (_, i) => (
          <div key={"empty" + i} />
        ))}
        {Array.from({ length: days }, (_, i) => {
          const date = month + "-" + String(i + 1).padStart(2, "0"),
            entries = items.filter((r) => r.valid_from?.startsWith(date));
          return (
            <div className="calendar-day" key={date}>
              <span>{i + 1}</span>
              {entries.slice(0, 3).map((r) => (
                <button key={r.id} onClick={() => onRead(r.id)} title={r.title}>
                  {r.title || r.kind}
                </button>
              ))}
              {entries.length > 3 && (
                <small>另有 {entries.length - 3} 条</small>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
