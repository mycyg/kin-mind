import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Activity,
  ArrowDownToLine,
  ArrowUpRight,
  Bell,
  BookOpen,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  Database,
  FileText,
  Fingerprint,
  GitBranch,
  Layers3,
  LoaderCircle,
  Menu,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import {
  api,
  commandId,
  initialToken,
  save,
  savedScope,
  saveScope,
  scopeWasSaved,
  setToken,
  stamp,
  type Scope,
} from "./api";
import { EventGraphPanel } from "./EventGraphPanel";
import { SealedEntries } from "./Sealed";
import { listScopes, type ScopeList } from "./scopes.mjs";
import {
  FamilyEditor,
  AttachmentPreview,
  DataControls,
  BudgetSettings,
  DeleteControl,
  TimelineCalendar,
} from "./Advanced";
import "./style.css";

const navigation = [
  ["overview", "总览", Activity],
  ["memories", "记忆浏览", Database],
  ["timeline", "时间线与连续性", Clock3],
  ["families", "主题与关系", GitBranch],
  ["knowledge", "知识与附件", BookOpen],
  ["diary", "日记与自述", FileText],
  ["conflicts", "冲突与纠正", ShieldCheck],
  ["recall", "召回实验室", Search],
  ["contact", "主动联系", Bell],
  ["settings", "设置", Settings2],
] as const;
const kinds: any = {
  episode: "经历",
  fact: "事实",
  state: "状态",
  preference: "偏好",
  procedure: "方法",
  relationship: "关系",
  commitment: "承诺",
  reminder: "提醒",
  prediction: "预测",
  diary: "日记",
  summary: "摘要",
  portrait: "画像",
  self_narrative: "自述",
  knowledge: "知识",
  checkpoint: "连续性",
  observation: "观察",
};
const statuses: any = {
  active: "有效",
  unverified: "待核实",
  superseded: "已替代",
  refuted: "已反驳",
  retracted: "已撤回",
  archived: "已归档",
  pending: "等待处理",
  complete: "已完成",
  running: "处理中",
  retry: "等待重试",
  waiting_config: "待配置",
  failed: "失败",
  canceled: "已取消",
  candidate: "候选",
  published: "已发布",
  scheduled: "已调度",
  queued: "已入队",
  suggested: "待发建议",
  ready: "待发送",
  // A delivery the host answered 2xx is in the host's queue, not yet with anybody (CR2-INT-07).
  sent: "已交给宿主",
  acknowledged: "已确认",
  uncertain: "投递不确定",
  paused: "已暂停",
};
type RecordItem = {
  id: string;
  title: string;
  kind: string;
  content?: string;
  status: string;
  revision: number;
  source_ids: string[];
  updated_at?: string;
  scope: Scope;
  [key: string]: any;
};

function Badge({ value }: { value: string }) {
  return (
    <span className={`badge ${value}`}>
      {statuses[value] ?? kinds[value] ?? value}
    </span>
  );
}
function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty">
      <Layers3 size={30} />
      <h3>{title}</h3>
      <p>{detail}</p>
    </div>
  );
}
const recordViews = ["memories", "timeline", "knowledge", "diary", "conflicts"];
const aborted = (e: unknown) => (e as { name?: string })?.name === "AbortError";
function useDebounced<T>(value: T, delay = 300) {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
// Each call cancels the request before it; the caller drops an answer whose signal was aborted.
function useLatest() {
  const current = useRef<AbortController | null>(null);
  return useCallback(() => {
    current.current?.abort();
    current.current = new AbortController();
    return current.current.signal;
  }, []);
}
const MORE_SCOPES = "__more_scopes__";
function App() {
  const [connected, setConnected] = useState(false),
    [token, setTokenInput] = useState(initialToken),
    [view, setView] = useState("overview"),
    [scopeInput, setScopeInput] = useState<Scope>(savedScope),
    [scopes, setScopes] = useState<ScopeList | null>(null),
    [error, setError] = useState(""),
    [pending, setPending] = useState(0),
    [overview, setOverview] = useState<any>({}),
    [page, setPage] = useState<{
      key: string;
      items: RecordItem[];
      cursor: string | null;
    }>({ key: "", items: [], cursor: null }),
    [browseInput, setBrowseInput] = useState(""),
    [selected, setSelected] = useState<RecordItem | null>(null),
    [revisions, setRevisions] = useState<any[]>([]),
    [source, setSource] = useState<any>(null),
    [drawerTab, setDrawerTab] = useState("content"),
    [importing, setImporting] = useState(false),
    [query, setQuery] = useState(""),
    [recallMode, setRecallMode] = useState("fast"),
    [recall, setRecall] = useState<any>(null),
    [families, setFamilies] = useState<any[]>([]),
    [graph, setGraph] = useState<any>(null),
    [family, setFamily] = useState(""),
    [contact, setContact] = useState<any>({
      policies: [],
      schedules: [],
      outbox: [],
      cursors: {},
    }),
    [models, setModels] = useState<any>({}),
    [revisionText, setRevisionText] = useState(""),
    [diary, setDiary] = useState<any>(null),
    [mobile, setMobile] = useState(false),
    [notice, setNotice] = useState("");
  const busy = pending > 0;
  // Typing settles before anything is read, and a list only ever shows the query it was read for.
  const scope = useDebounced(scopeInput),
    browseSearch = useDebounced(browseInput);
  const listKey = JSON.stringify([scope, view, browseSearch]);
  const items = page.key === listKey ? page.items : [];
  const latestRecords = useLatest(),
    latestFamilies = useLatest(),
    latestRead = useLatest(),
    latestDiary = useLatest();
  useEffect(() => saveScope(scope), [scope]);
  const run = useCallback(async (task: () => Promise<any>) => {
    setPending((n) => n + 1);
    setError("");
    try {
      return await task();
    } catch (e) {
      if (!aborted(e)) setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setPending((n) => n - 1);
    }
  }, []);
  const loadOverview = useCallback(async () => {
    setOverview(await api.call("overview"));
  }, []);
  const loadRecords = useCallback(
    async (next?: string) => {
      const key = JSON.stringify([scope, view, browseSearch]);
      const signal = latestRecords();
      const filter =
        view === "knowledge"
          ? { kind: "knowledge" }
          : view === "conflicts"
            ? { status: "unverified" }
            : {};
      const r = await api.call("list_memories", {
        query: {
          ...scope,
          ...filter,
          ...(["diary", "timeline"].includes(view) ? { group: view } : {}),
          ...(next ? { cursor: next } : {}),
          limit: 100,
          query: browseSearch,
        },
        signal,
      });
      if (signal.aborted) return;
      setPage((previous) =>
        !next
          ? { key, items: r.items, cursor: r.cursor }
          : previous.key === key
            ? { key, items: [...previous.items, ...r.items], cursor: r.cursor }
            : previous,
      );
    },
    [scope, view, browseSearch, latestRecords],
  );
  // Kin's own diary (kin-reflection), each entry with the owner's replies, and her dreams: the
  // narrative records listed below never held one of her entries.
  const loadDiary = useCallback(
    async (next?: number) => {
      const signal = latestDiary();
      const r = await api.call("read_kin_diary", {
        query: { ...scope, limit: 20, ...(next ? { cursor: next } : {}) },
        signal,
      });
      if (signal.aborted) return;
      setDiary((previous: any) =>
        next && previous
          ? { ...r, entries: [...previous.entries, ...r.entries], dreams: previous.dreams }
          : r,
      );
    },
    [scope, latestDiary],
  );
  const loadContact = useCallback(async (table?: string, cursor?: string) => {
    const tables = table ? [table] : ["policies", "schedules", "outbox"];
    const rows = await Promise.all(
      tables.map((t) =>
        api.call("list_contact", {
          path: { table: t },
          query: { limit: 50, order: "recent", ...(cursor ? { cursor } : {}) },
        }),
      ),
    );
    setContact((previous: any) => {
      const next = { ...previous, cursors: { ...previous.cursors } };
      tables.forEach((t, i) => {
        next[t] = cursor ? [...previous[t], ...rows[i].items] : rows[i].items;
        next.cursors[t] = rows[i].cursor;
      });
      return next;
    });
  }, []);
  const loadFamilies = useCallback(async () => {
    const signal = latestFamilies();
    const [f, g] = await Promise.all([
      api.call("list_families", { query: scope, signal }),
      api.call("read_graph", {
        query: {
          ...scope,
          ...(family ? { family_id: family } : {}),
          limit: 150,
        },
        signal,
      }),
    ]);
    if (signal.aborted) return;
    setFamilies(f.items);
    setGraph(g);
  }, [scope, family, latestFamilies]);
  const loadView = useCallback(async () => {
    if (recordViews.includes(view)) await loadRecords();
    if (view === "diary") await loadDiary();
    if (view === "families") await loadFamilies();
    if (view === "contact") await loadContact();
    if (view === "settings")
      setModels(await api.call("read_settings", { path: { key: "models" } }));
  }, [view, loadRecords, loadDiary, loadFamilies, loadContact]);
  const refresh = useCallback(() => {
    // The picker's list is read again the next time it is opened: scopes come and go.
    setScopes(null);
    return Promise.all([loadOverview(), loadView()]);
  }, [loadOverview, loadView]);
  const adoptedScope = useRef(scopeWasSaved);
  const connect = () =>
    run(async () => {
      setToken(token);
      const health: any = await api.call("health");
      // A console never pointed at a scope opens on the deployment's own, once (E3-03).
      if (!adoptedScope.current && health?.default_scope) {
        adoptedScope.current = true;
        // An equal scope must keep its identity: debouncing a fresh copy would reload
        // the current view and clear a graph pick made just after connecting.
        setScopeInput(previous =>
          (Object.keys(previous) as (keyof Scope)[]).every(key => previous[key] === health.default_scope[key])
            ? previous : health.default_scope);
      }
      setConnected(true);
    });
  // Every page, following the cursor, up to twenty pages a time; what is left past that is
  // one choice away (CR-MEM-13).
  const loadScopes = (more = false) => {
    if (scopes !== null && !more) return;
    void run(async () =>
      setScopes(
        await listScopes(
          async (query) => (await api.call("list_scopes", { query })) as any,
          more && scopes ? { cursor: scopes.cursor, items: scopes.items } : {},
        ),
      ),
    );
  };
  useEffect(() => {
    if (initialToken) void connect();
  }, []);
  useEffect(() => {
    if (connected) void run(loadView);
  }, [connected, loadView, run]);
  // The overview counts the whole store: read it on connect, then once a minute while the page
  // is in view, and again when it comes back into view.
  useEffect(() => {
    if (!connected) return;
    void run(loadOverview);
    const tick = () => {
      if (document.visibilityState === "visible")
        void loadOverview().catch(() => {});
    };
    const timer = setInterval(tick, 60000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [connected, loadOverview, run]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSelected(null);
        setImporting(false);
        setMobile(false);
      }
      if (
        e.key === "/" &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement)
      ) {
        e.preventDefault();
        setView("recall");
        setTimeout(
          () =>
            document.querySelector<HTMLInputElement>("#recall-query")?.focus(),
          0,
        );
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, []);
  // Opening a record reads it and its history, and writes nothing.
  const read = useCallback(
    (id: string) => {
      void run(async () => {
        const signal = latestRead();
        const [r, h] = await Promise.all([
          api.call("read_memory", { path: { record_id: id }, signal }),
          api.call("read_revisions", { path: { record_id: id }, signal }),
        ]);
        if (signal.aborted) return;
        setSelected(r);
        setRevisionText(r.content ?? "");
        setDrawerTab("content");
        setSource(null);
        setRevisions(h.items);
      });
    },
    [run, latestRead],
  );
  // A continued page belongs to the revision already shown, or it is not joined to it.
  const readOn = (record: RecordItem, piece: any) => {
    if (piece.revision !== record.revision)
      throw new Error("记忆已更新，请重新打开这条记录。");
    return {
      ...record,
      ...piece,
      content: (record.content ?? "") + piece.content,
    };
  };
  const revise = (action: string, extra: any = {}) =>
    run(async () => {
      if (!selected) return;
      const r = await api.call("revise_memory", {
        path: { record_id: selected.id },
        body: {
          expected_revision: selected.revision,
          command_id: commandId(),
          action: action as any,
          ...extra,
        },
      });
      setSelected(r);
      setRevisionText(r.content);
      setNotice("修订已保存");
      setRevisions(
        (await api.call("read_revisions", { path: { record_id: r.id } })).items,
      );
      await refresh();
    });
  const maintain = (kind: string) =>
    run(async () => {
      await api.call("run_maintenance", {
        body: { kind: kind as any, scope, command_id: commandId() },
      });
      setNotice("任务已加入处理队列");
      await loadOverview();
    });
  const download = async (sid: string) => {
    const r = await api.call("read_source", { path: { source_id: sid } });
    await save(
      await api.response("read_attachment", { path: { source_id: sid } }),
      r.title || "attachment",
      r.media_type,
    );
  };
  if (!connected)
    return (
      <main className="login">
        <div className="login-card">
          <Logo />
          <p className="eyebrow">YOUR MEMORY, IN CONTEXT</p>
          <h1>
            记得有据，
            <br />
            相处有续。
          </h1>
          <p>连接本地 MemoryPalace 服务，管理经历、关系与知识。</p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void connect();
            }}
          >
            <label>
              本地访问凭据
              <input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="粘贴 local-token 文件中的凭据"
                required
              />
            </label>
            <button className="primary" disabled={busy}>
              连接记忆库 <ArrowUpRight size={16} />
            </button>
          </form>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <small>
            控制台连接正在运行的记忆服务，不另起服务；凭据在该服务记忆库目录的
            local-token 文件中。
          </small>
        </div>
      </main>
    );
  const title = navigation.find((n) => n[0] === view)?.[1];
  return (
    <div className="app">
      <aside className={mobile ? "sidebar open" : "sidebar"}>
        <Logo />
        <div className="workspace">
          <span className="avatar">
            <Fingerprint size={22} />
          </span>
          <div>
            <strong>我的记忆空间</strong>
            <small>单用户 · 本地存储</small>
          </div>
          <span className="online" />
        </div>
        <span className="nav-label">记忆工作台</span>
        <nav>
          {navigation.map(([key, label, Icon]) => (
            <button
              key={key}
              className={view === key ? "nav-item active" : "nav-item"}
              onClick={() => {
                setView(key);
                setMobile(false);
                setQuery("");
                setRecall(null);
              }}
            >
              <Icon size={18} />
              {label}
              {key === "overview" && overview.jobs?.failed > 0 && (
                <span className="nav-count" title="失败的后台任务">
                  {overview.jobs.failed}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className="online" />
          <div>
            服务已连接<small>MemoryPalace 1.0</small>
          </div>
          <button aria-label="刷新" onClick={() => void run(refresh)}>
            <RefreshCw size={15} />
          </button>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu"
              aria-label="菜单"
              onClick={() => setMobile(!mobile)}
            >
              <Menu />
            </button>
            <span>记忆空间</span>
            <ChevronRight size={13} />
            <strong>{title}</strong>
          </div>
          <div className="top-actions">
            <span className="local-label">
              <ShieldCheck size={14} /> 本地私有
            </span>
            <button
              className="icon-button"
              aria-label="主动联系"
              onClick={() => setView("contact")}
            >
              <Bell size={17} />
            </button>
            <span className="small-avatar">M</span>
          </div>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                {view === "overview"
                  ? "MEMORY, WITH CONTINUITY"
                  : "MEMORYPALACE / " + view.toUpperCase()}
              </p>
              <h1>{view === "overview" ? "每一段经历，都有来处。" : title}</h1>
              <p>
                {view === "overview"
                  ? "从过去的经验，走向此刻的上下文。"
                  : `项目 ${scope.project} · 角色 ${scope.persona} · ${scope.world === "real" ? "现实领域" : scope.world}`}
              </p>
            </div>
            <button className="primary" onClick={() => setImporting(true)}>
              <Plus size={16} /> 添加来源
            </button>
          </div>
          <div className="scope-bar">
            <span>
              <CircleDot size={14} /> 当前范围
            </span>
            {(["project", "persona", "collection", "world"] as const).map(
              (key, i) => (
                <label key={key}>
                  {["项目", "角色", "知识库", "领域"][i]}
                  <input
                    aria-label={["项目", "角色", "知识库", "领域"][i]}
                    value={scopeInput[key]}
                    onChange={(e) =>
                      setScopeInput({ ...scopeInput, [key]: e.target.value })
                    }
                  />
                </label>
              ),
            )}
            <label>
              已有范围
              <select
                aria-label="已有范围"
                value=""
                onFocus={() => loadScopes()}
                onChange={(e) => {
                  if (e.target.value === MORE_SCOPES) loadScopes(true);
                  else if (e.target.value) setScopeInput(JSON.parse(e.target.value));
                }}
              >
                <option value="">选择…</option>
                {(scopes?.items ?? []).map((s) => (
                  <option key={JSON.stringify(s)} value={JSON.stringify(s)}>
                    {s.project} / {s.persona} / {s.collection} / {s.world}
                  </option>
                ))}
                {scopes?.cursor && <option value={MORE_SCOPES}>加载更多范围…</option>}
              </select>
            </label>
          </div>
          {error && (
            <div className="error banner" role="alert">
              {error}
              <button aria-label="关闭错误" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              <Check size={15} />
              {notice}
              <button aria-label="关闭通知" onClick={() => setNotice("")}>
                <X size={14} />
              </button>
            </div>
          )}
          {view === "overview" && (
            <>
              <div className="metrics">
                {[
                  [
                    Database,
                    "记忆对象",
                    overview.records ?? 0,
                    "经历、事实与知识",
                  ],
                  [
                    Layers3,
                    "来源快照",
                    overview.sources ?? 0,
                    "保留可追溯的原始内容",
                  ],
                  [
                    Activity,
                    "快速召回 p95",
                    overview.latency?.p95_ms == null
                      ? "—"
                      : `${overview.latency.p95_ms.toFixed(1)} ms`,
                    `${overview.latency?.samples ?? 0} 次近期调用`,
                  ],
                  [Sparkles, "待整理", overview.dirty ?? 0, "按变更增量处理"],
                ].map(([Icon, label, value, caption], i) => {
                  const I = Icon as typeof Database;
                  return (
                    <div className="metric" key={i}>
                      <div className="metric-label">
                        {String(label)}
                        <I size={17} />
                      </div>
                      <strong>
                        {typeof value === "number"
                          ? value.toLocaleString()
                          : String(value)}
                      </strong>
                      <span>{String(caption)}</span>
                    </div>
                  );
                })}
              </div>
              <div className="overview-grid">
                <section className="panel">
                  <div className="panel-title">
                    <div>
                      <h2>记忆构成</h2>
                      <p>经历、关系、方法和知识，在同一空间中关联。</p>
                    </div>
                    <span className="label-muted">
                      {Object.keys(overview.kinds ?? {}).length} 种类型
                    </span>
                  </div>
                  <div className="composition">
                    {Object.entries(overview.kinds ?? {}).length ? (
                      Object.entries(overview.kinds).map(
                        ([kind, count]: any) => (
                          <button
                            className="composition-row"
                            key={kind}
                            onClick={() =>
                              setView(
                                kind === "knowledge" ? "knowledge" : "memories",
                              )
                            }
                          >
                            <span className="kind-icon">
                              <FileText size={16} />
                            </span>
                            <strong>{kinds[kind] ?? kind}</strong>
                            <div className="bar-track">
                              <i
                                style={{
                                  width: `${Math.max(3, (count / Math.max(1, overview.records)) * 100)}%`,
                                }}
                              />
                            </div>
                            <span>{count.toLocaleString()}</span>
                          </button>
                        ),
                      )
                    ) : (
                      <Empty
                        title="从一段记忆开始"
                        detail="添加聊天、工具记录或文档，建立可追溯的记忆。"
                      />
                    )}
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setView("memories")}
                  >
                    浏览全部记忆 <ArrowUpRight size={15} />
                  </button>
                </section>
                <section className="continuity-card">
                  <span className="eyebrow">PICK UP WHERE YOU LEFT OFF</span>
                  <div className="orbit-art">
                    <span />
                    <span />
                    <span />
                    <Fingerprint size={46} />
                  </div>
                  <h2>
                    让下一次对话，
                    <br />
                    接得上这一次。
                  </h2>
                  <p>保存当前目标、已确认进度、未完成承诺与下次入口。</p>
                  <button onClick={() => setView("timeline")}>
                    查看连续性 <ArrowUpRight size={16} />
                  </button>
                </section>
              </div>
              <section className="panel">
                <div className="panel-title">
                  <div>
                    <h2>后台处理</h2>
                    <p>解析、抽取与索引进度分别记录。</p>
                  </div>
                  <button
                    className="subtle"
                    onClick={() => void maintain("organize")}
                  >
                    <RefreshCw size={14} /> 整理当前范围
                  </button>
                </div>
                <JobPanel
                  counts={overview.jobs ?? noCounts}
                  run={run}
                  onChanged={loadOverview}
                />
              </section>
              <div className="foot-metrics">
                <span>索引版本 {overview.generation ?? 0}</span>
                <span>
                  近期模型 token {(overview.model_tokens ?? 0).toLocaleString()}
                </span>
                <span>
                  按配置单价估算费用 ${(overview.model_cost ?? 0).toFixed(4)}
                </span>
              </div>
            </>
          )}
          {view === "diary" && diary && (
            <KinDiary
              data={diary}
              scope={scope}
              run={run}
              onSaved={async () => {
                setNotice("回复已保存，Kin 下一次评估时会读到");
                await loadDiary();
              }}
              onMore={(cursor) => void run(() => loadDiary(cursor))}
            />
          )}
          {["memories", "timeline", "knowledge", "diary", "conflicts"].includes(
            view,
          ) && (
            <section className="panel">
              <label className="browse-search">
                搜索当前范围
                <input
                  type="search"
                  value={browseInput}
                  onChange={(e) => setBrowseInput(e.target.value)}
                  placeholder="标题、内容或标识符"
                />
              </label>
              <div className="panel-title">
                <div>
                  <h2>
                    {view === "conflicts"
                      ? "待核实记录"
                      : view === "timeline"
                        ? "经历与未完成事项"
                        : view === "diary"
                          ? "叙事记录"
                          : "全部记录"}
                  </h2>
                  <p>
                    {view === "conflicts"
                      ? "核对来源后确认、纠正或撤回。"
                      : view === "knowledge"
                        ? "按文档版本、位置和原始附件追溯。"
                        : "有效状态与历史修订分别保留。"}
                  </p>
                </div>
                {view === "diary" ? (
                  <div className="button-row">
                    {["diary", "portrait", "self_narrative"].map((k) => (
                      <button
                        className="subtle"
                        key={k}
                        onClick={() => void maintain(k)}
                      >
                        生成{kinds[k]}
                      </button>
                    ))}
                  </div>
                ) : (
                  <span className="label-muted">{items.length} 条已加载</span>
                )}
              </div>
              <VirtualRecords
                items={items}
                loading={page.key !== listKey}
                onRead={read}
              />
              {page.key === listKey && page.cursor && (
                <button
                  className="load-more"
                  onClick={() => void run(() => loadRecords(page.cursor!))}
                >
                  加载更多
                </button>
              )}
            </section>
          )}
          {view === "diary" && (
            <SealedEntries scope={scope} run={run} notice={setNotice} />
          )}
          {view === "families" && (
            <>
              <div className="button-row section-toolbar">
                <select
                  aria-label="主题选择"
                  value={family}
                  onChange={(e) => setFamily(e.target.value)}
                >
                  <option value="">当前范围</option>
                  {families.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.title}
                    </option>
                  ))}
                </select>
                <button
                  className="subtle"
                  onClick={() => void maintain("organize")}
                >
                  运行增量整理
                </button>
              </div>
              <EventGraphPanel
                data={graph}
                scope={scope}
                family={family}
                onSource={read}
              />
              <FamilyEditor
                families={families}
                scope={scope}
                run={run}
                refresh={loadFamilies}
              />
              <div className="family-grid">
                {families.map((f) => (
                  <section className="panel family" key={f.id}>
                    <div className="panel-title">
                      <Layers3 size={18} />
                      <Badge value={f.state} />
                    </div>
                    <h3>{f.title}</h3>
                    <p>
                      {f.members.length} 个成员 · revision {f.revision}
                    </p>
                    <div className="button-row">
                      <button
                        className="text-button"
                        onClick={() => setFamily(f.id)}
                      >
                        查看成员
                      </button>
                      {f.state === "candidate" && (
                        <button
                          className="subtle"
                          onClick={() =>
                            void run(async () => {
                              await api.call("change_family", {
                                path: { family_id: f.id },
                                body: {
                                  expected_revision: f.revision,
                                  action: "publish",
                                },
                              });
                              await loadFamilies();
                            })
                          }
                        >
                          发布
                        </button>
                      )}
                    </div>
                  </section>
                ))}
              </div>
            </>
          )}
          {view === "recall" && (
            <section className="panel recall-panel">
              <h2>一次召回，逐步展开。</h2>
              <p>查看候选来源、过滤理由、融合排序与最终上下文。</p>
              <form
                className="recall-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const data = new FormData(e.currentTarget);
                  void run(async () =>
                    setRecall(
                      await api.call("recall", {
                        body: {
                          query,
                          scope,
                          scenario: data.get("scenario") as any,
                          mode: recallMode as any,
                          budget: Number(data.get("budget")),
                          history: data.get("history") === "on",
                          explain: true,
                          at: String(data.get("at") || "") || undefined,
                          known_at:
                            String(data.get("known_at") || "") || undefined,
                        },
                      }),
                    ),
                  );
                }}
              >
                <div className="search-field">
                  <Search size={19} />
                  <input
                    id="recall-query"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="关于这个问题，过去有哪些相关经验？"
                    required
                  />
                  <button className="primary" disabled={busy}>
                    召回 <ArrowUpRight size={16} />
                  </button>
                </div>
                <div className="recall-options">
                  <label>
                    场景
                    <select name="scenario">
                      <option value="tool">工具协作</option>
                      <option value="companion">陪伴</option>
                      <option value="knowledge">知识问答</option>
                      <option value="research">研究</option>
                      <option value="creative">创作</option>
                      <option value="support">客服</option>
                      <option value="operations">运维</option>
                    </select>
                  </label>
                  <label>
                    路径
                    <select
                      name="mode"
                      value={recallMode}
                      onChange={(e) => setRecallMode(e.target.value)}
                    >
                      <option value="fast">快速</option>
                      <option value="deep">深度</option>
                    </select>
                  </label>
                  <label>
                    预算
                    <input
                      name="budget"
                      type="number"
                      defaultValue="2000"
                      min="0"
                      max="32000"
                    />
                  </label>
                  <label className="checkbox">
                    <input name="history" type="checkbox" />
                    包含历史状态
                  </label>
                  <label>
                    有效时间
                    <input name="at" placeholder="ISO 8601，可选" />
                  </label>
                  <label>
                    获知时间
                    <input name="known_at" placeholder="ISO 8601，可选" />
                  </label>
                </div>
                {recallMode === "deep" && (
                  <p className="form-help">
                    这里的召回不带会话，只读不记：深度路径不调用模型、不计费，只用已有缓存或原文；需要模型处理的部分会注明需要会话。
                  </p>
                )}
              </form>
              {recall && (
                <>
                  <div className="recall-stats">
                    <span>{recall.items?.length ?? 0} 条记录</span>
                    <span>
                      {recall.tokens} / {recall.budget ?? "—"} token
                    </span>
                    {typeof recall.latency_ms === "number" && (
                      <span>{recall.latency_ms.toFixed(1)} ms</span>
                    )}
                    <span>索引版本 {recall.generation}</span>
                  </div>
                  <pre className="context-output">
                    {recall.text || "当前范围内未找到可使用的记忆。"}
                  </pre>
                  <div className="trace-grid">
                    <Trace title="候选通道" data={recall.trace?.channels} />
                    <Trace title="过滤理由" data={recall.trace?.filtered} />
                    <Trace title="融合排序" data={recall.trace?.ranked} />
                  </div>
                  {(recall.items ?? []).map((r: any) =>
                    // A kin context's index also names derived views, read at a digest
                    // rather than a record revision; only records open in the drawer.
                    typeof r.revision === "string" ? (
                      <span className="result-link derived" key={r.id}>
                        <Layers3 size={15} />
                        {r.id}
                      </span>
                    ) : (
                      <button
                        className="result-link"
                        key={r.id}
                        onClick={() => read(r.id)}
                      >
                        <FileText size={15} />
                        {r.title || r.id}
                        {r.status && <Badge value={r.status} />}
                        <ArrowUpRight size={14} />
                      </button>
                    ),
                  )}
                </>
              )}
            </section>
          )}
          {view === "contact" && (
            <ContactView
              data={contact}
              scope={scope}
              onRefresh={() => run(() => loadContact())}
              onMore={(table: string) =>
                run(() => loadContact(table, contact.cursors[table]))
              }
              run={run}
              notice={setNotice}
            />
          )}
          {view === "settings" && (
            <>
              <SettingsView
                models={models}
                setModels={setModels}
                run={run}
                notice={setNotice}
                scope={scope}
              />
              <BudgetSettings run={run} notice={setNotice} />
              <DataControls run={run} notice={setNotice} />
            </>
          )}
          {view === "timeline" && (
            <TimelineCalendar items={items} onRead={read} />
          )}
        </div>
        <footer>
          MemoryPalace <span>来源可追溯 · 状态可修订 · 上下文有预算</span>
          <span>1.0</span>
        </footer>
      </main>
      {busy && (
        <div className="busy-indicator" role="status">
          <LoaderCircle size={15} />
          正在处理
        </div>
      )}
      {importing && (
        <ImportDialog
          scope={scope}
          error={error}
          onClose={() => setImporting(false)}
          onSubmit={async (data, file, url) => {
            const result = await run(async () => {
              if (url)
                await api.call("import_url", {
                  body: { url, scope, title: data.title },
                });
              else if (file) await api.upload(file, data, file.name);
              else await api.call("receive_source", { body: data as any });
              await refresh();
              return true;
            });
            if (result) {
              setImporting(false);
              setNotice(url ? "网页已导入" : "来源已可靠接收");
            }
          }}
        />
      )}
      {selected && (
        <div
          className="drawer-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setSelected(null);
          }}
        >
          <aside
            className="drawer"
            role="dialog"
            aria-modal="true"
            aria-label="记忆详情"
          >
            <div className="drawer-top">
              <span className="eyebrow">MEMORY DETAIL</span>
              <button aria-label="关闭详情" onClick={() => setSelected(null)}>
                <X size={21} />
              </button>
            </div>
            <div className="button-row">
              {selected.kind && <Badge value={selected.kind} />}
              {selected.status && <Badge value={selected.status} />}
              <span className="label-muted">r{selected.revision}</span>
            </div>
            <h2>{selected.title || kinds[selected.kind] || selected.id}</h2>
            <p className="record-id">{selected.id}</p>
            <div className="tabs">
              {[
                ["content", "内容"],
                ["sources", "来源"],
                ["revisions", "修订"],
                ["correct", "纠正"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  className={drawerTab === key ? "selected" : ""}
                  onClick={() => {
                    if (key !== "correct") { setDrawerTab(key); return; }
                    void run(async () => {
                      let complete: RecordItem = selected;
                      while (complete.cursor) {
                        const piece = await api.call("read_memory", {path:{record_id:complete.id},query:{offset:complete.cursor}});
                        complete = readOn(complete, piece);
                      }
                      setSelected(complete);
                      setRevisionText(complete.content ?? "");
                      setDrawerTab("correct");
                    });
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            {drawerTab === "content" && (
              <>
                <div className="prose">{selected.content}</div>
                <dl className="detail-meta">
                  <dt>领域</dt>
                  <dd>{selected.scope?.world ?? "—"}</dd>
                  <dt>确认程度</dt>
                  <dd>{selected.confirmation}</dd>
                  <dt>生成内容</dt>
                  <dd>{selected.generated ? "是" : "否"}</dd>
                  <dt>独立来源</dt>
                  <dd>
                    {selected.independent_sources ??
                      selected.source_ids?.length ??
                      0}
                  </dd>
                  <dt>位置</dt>
                  <dd>{JSON.stringify(selected.locator ?? null)}</dd>
                </dl>
                {selected.cursor && (
                  <button
                    className="subtle"
                    onClick={() =>
                      void run(async () => {
                        const shown = selected;
                        const more = await api.call("read_memory", {
                          path: { record_id: shown.id },
                          query: { offset: shown.cursor },
                        });
                        const joined = readOn(shown, more);
                        setSelected((current) =>
                          current === shown ? joined : current,
                        );
                      })
                    }
                  >
                    继续读取
                  </button>
                )}
                <div className="button-row">
                  <button
                    className="subtle"
                    onClick={() =>
                      void revise(
                        selected.status === "archived" ? "restore" : "archive",
                      )
                    }
                  >
                    {selected.status === "archived" ? "恢复" : "归档"}
                  </button>
                  <button
                    className="subtle"
                    onClick={() => void revise("confirm")}
                  >
                    确认有效
                  </button>
                  <button
                    className="subtle danger"
                    onClick={() => void revise("retract")}
                  >
                    撤回结论
                  </button>
                </div>
                <DeleteControl
                  id={selected.id}
                  run={run}
                  onDeleted={() => {
                    setSelected(null);
                    void run(refresh);
                  }}
                />
              </>
            )}
            {drawerTab === "sources" && (
              <>
                {(selected.source_ids ?? []).map((sid) => (
                  <div key={sid} className="source-row">
                    <button
                      onClick={() =>
                        void run(async () =>
                          setSource(
                            await api.call("read_source", {
                              path: { source_id: sid },
                            }),
                          ),
                        )
                      }
                    >
                      <FileText size={17} />
                      {sid.slice(0, 24)}…
                    </button>
                    <button
                      aria-label="下载来源"
                      onClick={() => void run(() => download(sid))}
                    >
                      <ArrowDownToLine size={16} />
                    </button>
                  </div>
                ))}
                {source && (
                  <>
                    <AttachmentPreview
                      source={source}
                      locator={selected.locator}
                      run={run}
                    />
                    <Trace title={source.title || "来源元数据"} data={source} />
                  </>
                )}
              </>
            )}
            {drawerTab === "revisions" &&
              revisions.map((r: any) => (
                <div className="revision" key={r.revision}>
                  <div>
                    <strong>
                      r{r.revision} · {r.action}
                    </strong>
                    <span>{stamp(r.changed_at)}</span>
                  </div>
                  <p>{r.data.content}</p>
                  <small>{r.reason}</small>
                  {r.revision !== selected.revision && (
                    <button
                      className="text-button"
                      onClick={() =>
                        void revise("rollback", { target_revision: r.revision })
                      }
                    >
                      恢复此修订
                    </button>
                  )}
                </div>
              ))}
            {drawerTab === "correct" && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void revise("correct", {
                    content: revisionText,
                    reason: "用户在管理台纠正",
                  });
                }}
              >
                <label>
                  更正后的内容
                  <textarea
                    rows={12}
                    value={revisionText}
                    onChange={(e) => setRevisionText(e.target.value)}
                    required
                  />
                </label>
                <p className="form-help">
                  保存后立即更新有效视图，原内容保留在修订记录中。
                </p>
                <button className="primary" disabled={busy}>
                  保存纠正 <Check size={16} />
                </button>
              </form>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
function Logo() {
  return (
    <div className="logo">
      <span className="logo-symbol">
        <Layers3 size={23} />
      </span>
      <strong>
        MemoryPalace<span>记忆宫殿</span>
      </strong>
    </div>
  );
}
function KinDiary({
  data,
  scope,
  run,
  onSaved,
  onMore,
}: {
  data: any;
  scope: Scope;
  run: (task: () => Promise<any>) => Promise<any>;
  onSaved: () => Promise<void>;
  onMore: (cursor: number) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // One command per draft: a retry of the same words is the same reply, other words are a new one.
  const commands = useRef<Record<string, { text: string; id: string }>>({});
  const replying = data.replies === "enabled";
  const dreams = data.dreams?.state === "enabled" ? data.dreams.dreams : null;
  const send = (entry: string) =>
    run(async () => {
      const text = (drafts[entry] ?? "").trim();
      if (!text) return;
      const held = commands.current[entry];
      const command =
        held && held.text === text ? held.id : commandId();
      commands.current[entry] = { text, id: command };
      await api.call("reply_to_diary", {
        body: {
          scope,
          request: { reflection_id: entry, text, command_id: command },
        },
      });
      delete commands.current[entry];
      setDrafts((previous) => ({ ...previous, [entry]: "" }));
      await onSaved();
    });
  return (
    <section className="panel kin-diary" aria-label="Kin 的日记">
      <div className="panel-title">
        <div>
          <h2>Kin 的日记</h2>
          <p>
            Kin 自己写下的想法，最新的在前。
            {replying ? "可以直接回复，回复会作为你的原话交给她。" : ""}
          </p>
        </div>
        <span className="label-muted">{data.entries.length} 篇已加载</span>
      </div>
      {data.entries.length === 0 ? (
        <Empty title="还没有日记" detail="Kin 写下日记后会出现在这里。" />
      ) : (
        <ol className="diary-entries">
          {data.entries.map((entry: any) => (
            <li className="diary-entry" key={entry.source_id}>
              <div className="diary-head">
                <strong>{entry.topic || "日记"}</strong>
                <span className="label-muted">{stamp(entry.at)}</span>
              </div>
              <p className="diary-text">{entry.text}</p>
              {entry.replies.map((reply: any) => (
                <blockquote className="diary-reply" key={reply.source_id}>
                  <span className="label-muted">回复 · {stamp(reply.at)}</span>
                  <p>{reply.text}</p>
                </blockquote>
              ))}
              {replying && (
                <form
                  className="diary-reply-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void send(entry.source_id);
                  }}
                >
                  <textarea
                    aria-label="回复这篇日记"
                    maxLength={2000}
                    rows={2}
                    value={drafts[entry.source_id] ?? ""}
                    onChange={(e) =>
                      setDrafts((previous) => ({
                        ...previous,
                        [entry.source_id]: e.target.value,
                      }))
                    }
                    placeholder="写点什么回给她"
                  />
                  <button
                    className="subtle"
                    disabled={!(drafts[entry.source_id] ?? "").trim()}
                  >
                    回复
                  </button>
                </form>
              )}
            </li>
          ))}
        </ol>
      )}
      {data.cursor && (
        <button className="load-more" onClick={() => onMore(data.cursor)}>
          加载更多日记
        </button>
      )}
      {dreams && (
        <section className="kin-dreams" aria-label="Kin 的梦">
          <h3>梦</h3>
          {dreams.length === 0 ? (
            <p className="label-muted">还没有梦。</p>
          ) : (
            <ol className="diary-entries">
              {dreams.map((dream: any) => (
                <li className="diary-entry dream" key={dream.source_id}>
                  <span className="label-muted">{stamp(dream.at)}</span>
                  <p className="diary-text">{dream.text}</p>
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
    </section>
  );
}
function VirtualRecords({
  items,
  loading,
  onRead,
}: {
  items: RecordItem[];
  loading: boolean;
  onRead: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({
    count: items.length,
    getScrollElement: () => ref.current,
    estimateSize: () => 90,
    overscan: 6,
  });
  if (!items.length)
    return loading ? (
      <Empty title="正在读取" detail="读取当前范围与筛选条件下的记录。" />
    ) : (
      <Empty
        title="当前范围还没有记录"
        detail="添加来源，或切换项目、角色和知识库。"
      />
    );
  return (
    <div className="record-list" ref={ref}>
      <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
        {virtual.getVirtualItems().map((row) => {
          const item = items[row.index];
          return (
            <button
              className="record-row"
              key={item.id}
              onClick={() => onRead(item.id)}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: row.size,
                transform: `translateY(${row.start}px)`,
              }}
            >
              <span className="record-icon">
                <FileText size={18} />
              </span>
              <div>
                <strong>{item.title || kinds[item.kind]}</strong>
                <p>{item.content?.slice(0, 110)}</p>
                <small>
                  {stamp(item.updated_at)} · {kinds[item.kind]} · r
                  {item.revision}
                </small>
              </div>
              <Badge value={item.status} />
              <ChevronRight size={16} />
            </button>
          );
        })}
      </div>
    </div>
  );
}
const jobStates = ["failed", "waiting_config", "retry", "running", "pending", "complete", "canceled"];
const noCounts: Record<string, number> = {};
function JobPanel({
  counts,
  run,
  onChanged,
}: {
  counts: Record<string, number>;
  run: (task: () => Promise<any>) => Promise<any>;
  onChanged: () => Promise<void>;
}) {
  const [chosen, setChosen] = useState(""),
    [list, setList] = useState<{ state: string; items: any[]; cursor: string | null }>({
      state: "",
      items: [],
      cursor: null,
    });
  // Job ids are hashes, so one unfiltered page is an arbitrary handful: read by state instead,
  // starting from the first state that needs attention.
  const state = chosen || jobStates.find((s) => counts[s] > 0) || "failed";
  const latest = useLatest();
  const load = useCallback(
    async (cursor?: string) => {
      const signal = latest();
      const r = await api.call("list_jobs", {
        query: { state, limit: 20, order: "recent", ...(cursor ? { cursor } : {}) },
        signal,
      });
      if (signal.aborted) return;
      setList((previous) => ({
        state,
        items: cursor && previous.state === state ? [...previous.items, ...r.items] : r.items,
        cursor: r.cursor,
      }));
    },
    [state, latest],
  );
  useEffect(() => {
    void run(() => load());
  }, [load, counts, run]);
  const act = (id: string, action: string) =>
    void run(async () => {
      await api.call("control_job", { path: { job_id: id, action } });
      await onChanged();
    });
  const jobs = list.state === state ? list.items : [];
  return (
    <>
      <div className="button-row" role="tablist" aria-label="任务状态">
        {jobStates.map((s) => (
          <button
            key={s}
            role="tab"
            aria-selected={s === state}
            className={s === state ? "subtle selected" : "subtle"}
            onClick={() => setChosen(s)}
          >
            {statuses[s] ?? s} {counts[s] ?? 0}
          </button>
        ))}
      </div>
      {!jobs.length ? (
        <Empty
          title="处理队列为空"
          detail="新的解析、抽取与整理任务会显示在这里。"
        />
      ) : (
        <div className="job-list">
          <div className="job-head">
            <span>任务</span>
            <span>状态</span>
            <span>尝试次数</span>
            <span>操作</span>
          </div>
          {jobs.map((j) => (
            <div className="job-row" key={j.id}>
              <div>
                <strong>{j.kind}</strong>
                <small>{j.error || j.id.slice(0, 26)}</small>
              </div>
              <Badge value={j.state} />
              <span>
                {j.attempts} / {j.max_attempts}
              </span>
              <div>
                {["failed", "waiting_config", "retry"].includes(j.state) && (
                  <button className="text-button" onClick={() => act(j.id, "retry")}>
                    重试
                  </button>
                )}
                {["pending", "running"].includes(j.state) && (
                  <button className="text-button" onClick={() => act(j.id, "cancel")}>
                    取消
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {list.state === state && list.cursor && (
        <button className="load-more" onClick={() => void run(() => load(list.cursor!))}>
          加载更多
        </button>
      )}
    </>
  );
}
function Trace({ title, data }: { title: string; data: any }) {
  return (
    <details className="trace" open>
      <summary>{title}</summary>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  );
}
function ImportDialog({
  scope,
  error,
  onClose,
  onSubmit,
}: {
  scope: Scope;
  error: string;
  onClose: () => void;
  onSubmit: (data: any, file: File | null, url?: string) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null),
    [sending, setSending] = useState(false),
    [webUrl, setWebUrl] = useState("");
  return (
    <div className="modal-backdrop">
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="添加来源"
      >
        <div className="panel-title">
          <div>
            <p className="eyebrow">ADD A SOURCE</p>
            <h2>留下一段可追溯的记忆。</h2>
          </div>
          <button aria-label="关闭导入" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setSending(true);
            const f = new FormData(e.currentTarget);
            try {
              if (webUrl) {
                await onSubmit(
                  { title: String(f.get("title") || "") },
                  null,
                  webUrl,
                );
                return;
              }
              await onSubmit(
                {
                  namespace: "console",
                  key: commandId(),
                  version: "1",
                  scope,
                  title: String(f.get("title") || file?.name || ""),
                  text: String(f.get("text") || ""),
                  kind: f.get("kind"),
                  authority: file ? "document" : "explicit",
                  extract: f.get("extract") === "on",
                  media_type:
                    file?.type ||
                    (file ? "application/octet-stream" : "text/plain"),
                },
                file,
              );
            } finally {
              setSending(false);
            }
          }}
        >
          <label>
            标题
            <input
              name="title"
              placeholder="一个经历、一条偏好，或一份资料"
              autoFocus
            />
          </label>
          <label>
            网页地址
            <input
              type="url"
              value={webUrl}
              onChange={(e) => setWebUrl(e.target.value)}
              placeholder="https://…"
            />
          </label>
          <div className="form-grid">
            <label>
              记录类型
              <select name="kind">
                {Object.entries(kinds).map(([key, label]: any) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              附件
              <input
                type="file"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
          </div>
          <label>
            内容
            <textarea
              name="text"
              rows={7}
              required={!file && !webUrl}
              placeholder="记录内容或选择附件上传。"
            />
          </label>
          <label className="checkbox">
            <input type="checkbox" name="extract" />
            在后台抽取结构化记忆
          </label>
          <p className="form-help">
            项目 {scope.project} · 角色 {scope.persona}
            。未配置所需模型时，保留来源并显示待配置状态。
          </p>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <div className="modal-actions">
            <button type="button" className="subtle" onClick={onClose}>
              取消
            </button>
            <button className="primary" disabled={sending}>
              保存来源 <Plus size={16} />
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
const newPolicy: Record<string, any> = {
  enabled: false,
  channel: null,
  timezone: "Asia/Singapore",
  quiet_start: 22,
  quiet_end: 8,
  max_per_day: 3,
  min_interval_minutes: 60,
  triggers: ["reminder", "commitment"],
  allowed_kinds: ["reminder", "commitment"],
  require_confirmation: true,
  idempotent_channel: false,
  greeting_text: "想聊聊今天的近况吗？",
};
function policyForm(f: FormData): Record<string, any> {
  return {
    enabled: f.get("enabled") === "on",
    channel: String(f.get("channel") || "") || null,
    timezone: String(f.get("timezone")),
    quiet_start: Number(f.get("start")),
    quiet_end: Number(f.get("end")),
    max_per_day: Number(f.get("max")),
    min_interval_minutes: Number(f.get("interval")),
    triggers: f.getAll("triggers"),
    allowed_kinds: f.getAll("allowed_kinds"),
    require_confirmation: f.get("confirm") === "on",
    idempotent_channel: f.get("idempotent") === "on",
    greeting_text: String(f.get("greeting_text") || "") || newPolicy.greeting_text,
  };
}
const sameScope = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
function ContactView({ data, scope, onRefresh, onMore, run, notice }: any) {
  // The policy being edited: its id, "" for a new one, null while the form is closed.
  const [editing, setEditing] = useState<string | null>(null);
  const stored = data.policies.find((p: any) => p.id === editing)?.data;
  const start: Record<string, any> = { ...newPolicy, ...(stored ?? {}) };
  const reminderPolicy =
    data.policies.find((p: any) => sameScope(p.data.scope, scope))?.id ??
    "default";
  return (
    <>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>联系策略</h2>
            <p>每个角色独立设置渠道、安静时段和发送确认。</p>
          </div>
          <button
            className="subtle"
            onClick={() =>
              setEditing(editing === null ? (data.policies[0]?.id ?? "") : null)
            }
          >
            <Settings2 size={15} />
            配置策略
          </button>
        </div>
        {data.policies.length ? (
          data.policies.map((p: any) => (
            <div className="policy-row" key={p.id}>
              <Bell size={19} />
              <div>
                <strong>{p.id}</strong>
                <p>
                  {p.data.scope.persona} · {p.data.timezone} ·{" "}
                  {p.data.quiet_start}:00–{p.data.quiet_end}:00 安静时段
                </p>
              </div>
              <Badge value={p.data.enabled ? "active" : "paused"} />
              <small>
                {p.data.require_confirmation ? "逐次确认" : "按策略发送"}
              </small>
            </div>
          ))
        ) : (
          <Empty
            title="发送策略尚未配置"
            detail="提醒保留为待发建议。配置渠道与策略后，才会发送。"
          />
        )}
        {editing !== null && (
          <form
            key={editing}
            className="settings-form"
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              const values = policyForm(f);
              const id = editing || String(f.get("id"));
              // Saving replaces the whole row, so what is sent is the row as the service holds it
              // now with only the fields changed here laid over it; its scope stays its own.
              const changed = Object.keys(values).filter(
                (k) => JSON.stringify(values[k]) !== JSON.stringify(start[k]),
              );
              void run(async () => {
                const current = (
                  await api.call("list_contact", {
                    path: { table: "policies" },
                    query: { limit: 200, order: "recent" },
                  })
                ).items.find((p: any) => p.id === id)?.data;
                await api.call("configure_contact", {
                  body: current
                    ? {
                        ...current,
                        ...Object.fromEntries(changed.map((k) => [k, values[k]])),
                        id,
                      }
                    : { ...values, id, scope },
                });
                await onRefresh();
                setEditing(null);
                notice("联系策略已保存");
              });
            }}
          >
            <label>
              编辑策略
              <select value={editing} onChange={(e) => setEditing(e.target.value)}>
                {data.policies.map((p: any) => (
                  <option key={p.id} value={p.id}>
                    {p.id}
                  </option>
                ))}
                <option value="">新建策略</option>
              </select>
            </label>
            {editing === "" && (
              <label>
                策略名称
                <input
                  name="id"
                  defaultValue={data.policies.length ? "" : "default"}
                  required
                />
              </label>
            )}
            <p className="form-help">
              范围 {(stored?.scope ?? scope).project} /{" "}
              {(stored?.scope ?? scope).persona}
              {stored ? "（保持该策略自己的范围）" : "（取当前范围）"}
            </p>
            <label>
              自主问候内容
              <input
                name="greeting_text"
                defaultValue={start.greeting_text}
                maxLength={2000}
              />
            </label>
            <fieldset>
              <legend>触发条件</legend>
              <div className="button-row">
                {[
                  ["reminder", "提醒"],
                  ["commitment", "承诺跟进"],
                  ["anniversary", "纪念日"],
                  ["checkin", "任务检查"],
                  ["greeting", "自主问候"],
                ].map(([k, v]) => (
                  <label className="checkbox" key={k}>
                    <input
                      name="triggers"
                      type="checkbox"
                      value={k}
                      defaultChecked={start.triggers.includes(k)}
                    />
                    {String(v)}
                  </label>
                ))}
              </div>
            </fieldset>
            <fieldset>
              <legend>可引用的记忆类型</legend>
              <div className="button-row">
                {Object.entries(kinds).map(([k, v]) => (
                  <label className="checkbox" key={k}>
                    <input
                      name="allowed_kinds"
                      type="checkbox"
                      value={k}
                      defaultChecked={start.allowed_kinds.includes(k)}
                    />
                    {String(v)}
                  </label>
                ))}
              </div>
            </fieldset>
            <label>
              最短间隔（分钟）
              <input
                name="interval"
                type="number"
                min="0"
                defaultValue={start.min_interval_minutes}
              />
            </label>
            <div className="form-grid">
              <label>
                回调地址
                <input
                  name="channel"
                  type="url"
                  defaultValue={start.channel ?? ""}
                  placeholder="https://your-host/callback"
                />
              </label>
              <label>
                时区
                <input name="timezone" defaultValue={start.timezone} required />
              </label>
              <label>
                每日上限
                <input
                  name="max"
                  type="number"
                  defaultValue={start.max_per_day}
                  min="0"
                  max="100"
                />
              </label>
              <label>
                安静时段开始
                <input
                  name="start"
                  type="number"
                  defaultValue={start.quiet_start}
                  min="0"
                  max="23"
                />
              </label>
              <label>
                安静时段结束
                <input
                  name="end"
                  type="number"
                  defaultValue={start.quiet_end}
                  min="0"
                  max="23"
                />
              </label>
            </div>
            <label className="checkbox">
              <input type="checkbox" name="enabled" defaultChecked={start.enabled} />
              启用发送
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                name="confirm"
                defaultChecked={start.require_confirmation}
              />
              发送前逐次确认
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                name="idempotent"
                defaultChecked={start.idempotent_channel}
              />
              回调支持 delivery id 去重
            </label>
            <button className="primary">保存策略</button>
          </form>
        )}
      </section>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>提醒与承诺</h2>
            <p>调度、暂停、延后与取消均保留记录。</p>
          </div>
          <button
            className="subtle"
            onClick={() =>
              void run(async () => {
                await api.call("contact_tick");
                await onRefresh();
                notice("待到期事项已检查");
              })
            }
          >
            检查到期事项
          </button>
        </div>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            void run(async () => {
              await api.call("create_schedule", {
                body: {
                  command_id: commandId(),
                  record_id: String(f.get("record")),
                  due_at: new Date(String(f.get("due"))).toISOString(),
                  policy_id: String(f.get("policy") || "default"),
                  recurrence: f.get("recurrence") as any,
                },
              });
              await onRefresh();
            });
          }}
        >
          <label>
            记忆 id
            <input name="record" placeholder="mem_…" required />
          </label>
          <label>
            到期时间
            <input type="datetime-local" name="due" required />
          </label>
          <label>
            策略
            <input name="policy" key={reminderPolicy} defaultValue={reminderPolicy} />
          </label>
          <label>
            重复
            <select name="recurrence">
              <option value="none">单次</option>
              <option value="daily">每天</option>
              <option value="weekly">每周</option>
              <option value="yearly">每年</option>
            </select>
          </label>
          <button className="primary">添加</button>
        </form>
        {data.schedules.map((s: any) => (
          <div className="schedule-row" key={s.id}>
            <Clock3 size={18} />
            <div>
              <strong>{s.record_id.slice(0, 25)}</strong>
              <p>
                {stamp(s.due_at)} · r{s.revision}
              </p>
            </div>
            <Badge value={s.state} />
            <div className="button-row">
              {["confirm", "pause", "resume", "snooze", "cancel"].map(
                (action) => (
                  <button
                    className="text-button"
                    key={action}
                    onClick={() =>
                      void run(async () => {
                        await api.call("change_schedule", {
                          path: { schedule_id: s.id },
                          body: {
                            expected_revision: s.revision,
                            action: action as any,
                            ...(action === "snooze"
                              ? {
                                  due_at: new Date(
                                    Date.now() + 3600000,
                                  ).toISOString(),
                                }
                              : {}),
                          },
                        });
                        await onRefresh();
                      })
                    }
                  >
                    {
                      {
                        confirm: "确认",
                        pause: "暂停",
                        resume: "恢复",
                        snooze: "延后一小时",
                        cancel: "取消",
                      }[action]
                    }
                  </button>
                ),
              )}
            </div>
          </div>
        ))}
        {data.cursors?.schedules && (
          <button className="load-more" onClick={() => void onMore("schedules")}>
            加载更多
          </button>
        )}
      </section>
      <section className="panel">
        <div className="panel-title">
          <h2>待发与投递记录</h2>
          <span className="label-muted">稳定 delivery id</span>
        </div>
        {data.outbox.length ? (
          data.outbox.map((d: any) => (
            <div className="outbox-row" key={d.id}>
              <div>
                <strong>{d.data.text}</strong>
                <small>{d.id}</small>
              </div>
              <Badge value={d.state} />
            </div>
          ))
        ) : (
          <Empty
            title="没有待发内容"
            detail="到期提醒与主动联系建议会显示在这里。"
          />
        )}
        {data.cursors?.outbox && (
          <button className="load-more" onClick={() => void onMore("outbox")}>
            加载更多
          </button>
        )}
      </section>
    </>
  );
}
function SettingsView({ models, setModels, run, notice, scope }: any) {
  const [role, setRole] = useState("extraction");
  const current = models[role] ?? {};
  return (
    <>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>模型角色</h2>
            <p>兼容 API 端点与本地端点，多个角色可使用同一模型。</p>
          </div>
          <Badge value="active" />
        </div>
        <div className="model-grid">
          <div className="model-nav">
            {[
              "extraction",
              "conflict",
              "summary",
              "rerank",
              "embedding",
              "vision",
              "asr",
              "prediction",
              "visual_embedding",
              "query",
              "answer",
              "judge",
            ].map((k) => (
              <button
                key={k}
                className={role === k ? "selected" : ""}
                onClick={() => setRole(k)}
              >
                <CircleDot size={14} />
                {k}
                <span>{models[k] ? "已配置" : "未配置"}</span>
              </button>
            ))}
          </div>
          <form
            key={role}
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget),
                vector = ["embedding", "visual_embedding"].includes(role);
              const edited: Record<string, any> = {
                  protocol: String(f.get("protocol")),
                  endpoint: String(f.get("endpoint")),
                  model: String(f.get("model")),
                  api_key_env: String(f.get("env") || "") || null,
                  timeout_seconds: Number(f.get("timeout")),
                  ...(vector ? { dimensions: Number(f.get("dimensions")) } : {}),
                },
                shown: Record<string, any> = {
                  protocol: current.protocol ?? "openai",
                  endpoint: current.endpoint ?? "",
                  model: current.model ?? "",
                  api_key_env: current.api_key_env ?? null,
                  timeout_seconds: current.timeout_seconds ?? 60,
                  ...(vector ? { dimensions: current.dimensions ?? 1024 } : {}),
                };
              const changed = Object.fromEntries(
                Object.keys(edited)
                  .filter((k) => JSON.stringify(edited[k]) !== JSON.stringify(shown[k]))
                  .map((k) => [k, edited[k]]),
              );
              void run(async () => {
                // Saving replaces every role at once: start from the roles as the service holds
                // them now and change only what was edited here in this one.
                const fresh = await api.call("read_settings", {
                  path: { key: "models" },
                });
                const updated = await api.call("configure_models", {
                  body: {
                    ...fresh,
                    [role]: fresh[role] ? { ...fresh[role], ...changed } : edited,
                  },
                });
                setModels(updated);
                notice("模型配置已保存，等待配置的任务可继续处理");
              });
            }}
          >
            <label>
              协议
              <select
                name="protocol"
                defaultValue={current.protocol ?? "openai"}
              >
                <option value="openai">OpenAI compatible</option>
                <option value="anthropic">Anthropic Messages</option>
              </select>
            </label>
            <label>
              API 端点
              <input
                name="endpoint"
                defaultValue={current.endpoint ?? ""}
                placeholder="http://127.0.0.1:8000/v1"
                required
              />
            </label>
            <label>
              模型名称
              <input name="model" defaultValue={current.model ?? ""} required />
            </label>
            <div className="form-grid">
              <label>
                密钥环境变量名
                <input
                  name="env"
                  defaultValue={current.api_key_env ?? ""}
                  placeholder="MEMORY_MODEL_KEY"
                />
              </label>
              <label>
                超时（秒）
                <input
                  type="number"
                  name="timeout"
                  defaultValue={current.timeout_seconds ?? 60}
                  min="1"
                  max="600"
                />
              </label>
            </div>
            {["embedding", "visual_embedding"].includes(role) && (
              <label>
                向量维度
                <input
                  name="dimensions"
                  type="number"
                  min="1"
                  max="8192"
                  defaultValue={current.dimensions ?? 1024}
                />
              </label>
            )}
            <p className="form-help">
              这里只保存环境变量名称。凭据由启动服务的环境提供。
            </p>
            <button className="primary">保存 {role}</button>
          </form>
        </div>
      </section>
      <section className="panel">
        <h2>数据维护</h2>
        <p>全文索引可重建；模型索引更新保留独立处理状态。</p>
        <div className="button-row">
          {[
            ["rebuild", "重建全文索引"],
            ["build_vectors", "构建向量索引"],
            ["organize", "整理当前范围"],
          ].map(([kind, label]) => (
            <button
              key={kind}
              className="subtle"
              onClick={() =>
                void run(async () => {
                  await api.call("run_maintenance", {
                    body: { kind: kind as any, scope, command_id: commandId() },
                  });
                  notice("维护任务已加入队列");
                })
              }
            >
              {label}
            </button>
          ))}
        </div>
      </section>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
