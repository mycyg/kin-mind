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
  scopeDefault,
  setToken,
  stamp,
  type Scope,
} from "./api";
import { EventGraphPanel } from "./EventGraphPanel";
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
  sent: "已发送",
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
function App() {
  const [connected, setConnected] = useState(false),
    [token, setTokenInput] = useState(initialToken),
    [view, setView] = useState("overview"),
    [scope, setScope] = useState<Scope>(scopeDefault),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [overview, setOverview] = useState<any>({}),
    [items, setItems] = useState<RecordItem[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [jobs, setJobs] = useState<any[]>([]),
    [selected, setSelected] = useState<RecordItem | null>(null),
    [revisions, setRevisions] = useState<any[]>([]),
    [source, setSource] = useState<any>(null),
    [drawerTab, setDrawerTab] = useState("content"),
    [importing, setImporting] = useState(false),
    [query, setQuery] = useState(""),
    [recall, setRecall] = useState<any>(null),
    [families, setFamilies] = useState<any[]>([]),
    [graph, setGraph] = useState<any>(null),
    [family, setFamily] = useState(""),
    [contact, setContact] = useState<any>({
      policies: [],
      schedules: [],
      outbox: [],
    }),
    [models, setModels] = useState<any>({}),
    [revisionText, setRevisionText] = useState(""),
    [mobile, setMobile] = useState(false),
    [notice, setNotice] = useState("");
  const scopeQuery = { ...scope };
  const run = useCallback(async (task: () => Promise<any>) => {
    setBusy(true);
    setError("");
    try {
      return await task();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  }, []);
  const loadOverview = useCallback(async () => {
    const [o, j] = await Promise.all([
      api.call("overview"),
      api.call("list_jobs", { query: { limit: 30 } }),
    ]);
    setOverview(o);
    setJobs(j.items);
  }, []);
  const [browseSearch, setBrowseSearch] = useState("");
  const loadRecords = useCallback(
    async (next?: string) => {
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
      });
      const filtered = r.items;
      setItems((previous) => (next ? [...previous, ...filtered] : filtered));
      setCursor(r.cursor);
    },
    [scope, view, browseSearch],
  );
  const loadContact = useCallback(async () => {
    const rows = await Promise.all(
      ["policies", "schedules", "outbox"].map((table) =>
        api.call("list_contact", { path: { table } }),
      ),
    );
    setContact({
      policies: rows[0].items,
      schedules: rows[1].items,
      outbox: rows[2].items,
    });
  }, []);
  const loadFamilies = useCallback(async () => {
    const [f, g] = await Promise.all([
      api.call("list_families", { query: scope }),
      api.call("read_graph", {
        query: {
          ...scope,
          ...(family ? { family_id: family } : {}),
          limit: 150,
        },
      }),
    ]);
    setFamilies(f.items);
    setGraph(g);
  }, [scope, family]);
  const refresh = useCallback(async () => {
    await loadOverview();
    if (
      ["memories", "timeline", "knowledge", "diary", "conflicts"].includes(view)
    )
      await loadRecords();
    if (view === "families") await loadFamilies();
    if (view === "contact") await loadContact();
    if (view === "settings")
      setModels(await api.call("read_settings", { path: { key: "models" } }));
  }, [view, loadOverview, loadRecords, loadFamilies, loadContact]);
  const connect = () =>
    run(async () => {
      setToken(token);
      await api.call("health");
      setConnected(true);
    });
  useEffect(() => {
    if (initialToken) void connect();
  }, []);
  useEffect(() => {
    if (connected) void run(refresh);
  }, [connected, refresh, run]);
  useEffect(() => {
    if (!connected) return;
    const timer = setInterval(() => {
      void loadOverview().catch(() => {});
    }, 10000);
    return () => clearInterval(timer);
  }, [connected, loadOverview]);
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
  const read = useCallback(
    (id: string) => {
      void run(async () => {
        const r = await api.call("read_memory", { path: { record_id: id } });
        setSelected(r);
        setRevisionText(r.content);
        setDrawerTab("content");
        setSource(null);
        const h = await api.call("read_revisions", { path: { record_id: id } });
        setRevisions(h.items);
        await api.call("record_feedback", {
          body: { record_id: id, type: "read", session: "console" },
        });
      });
    },
    [run],
  );
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
    const bytes = await api.call("read_attachment", {
      path: { source_id: sid },
    });
    const url = URL.createObjectURL(new Blob([bytes], { type: r.media_type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = r.title || "attachment";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
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
          <small>也可运行 eventmem console 自动连接。</small>
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
              {key === "conflicts" && overview.jobs?.failed > 0 && (
                <span className="nav-count">{overview.jobs.failed}</span>
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
                    value={scope[key]}
                    onChange={(e) =>
                      setScope({ ...scope, [key]: e.target.value })
                    }
                  />
                </label>
              ),
            )}
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
                <JobList
                  jobs={jobs}
                  onAction={(id, action) =>
                    void run(async () => {
                      await api.call("control_job", {
                        path: { job_id: id, action },
                      });
                      await loadOverview();
                    })
                  }
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
          {["memories", "timeline", "knowledge", "diary", "conflicts"].includes(
            view,
          ) && (
            <section className="panel">
              <label className="browse-search">
                搜索当前范围
                <input
                  type="search"
                  value={browseSearch}
                  onChange={(e) => setBrowseSearch(e.target.value)}
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
              <VirtualRecords items={items} onRead={read} />
              {cursor && (
                <button
                  className="load-more"
                  onClick={() => void run(() => loadRecords(cursor))}
                >
                  加载更多
                </button>
              )}
            </section>
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
              <EventGraphPanel data={graph} scope={scope} onSource={read} />
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
                          mode: data.get("mode") as any,
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
                    <select name="mode">
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
              </form>
              {recall && (
                <>
                  <div className="recall-stats">
                    <span>{recall.items.length} 条记录</span>
                    <span>
                      {recall.tokens} / {recall.budget} token
                    </span>
                    <span>{recall.latency_ms.toFixed(1)} ms</span>
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
                  {recall.items.map((r: any) => (
                    <button
                      className="result-link"
                      key={r.id}
                      onClick={() => read(r.id)}
                    >
                      <FileText size={15} />
                      {r.title || r.id}
                      <Badge value={r.status} />
                      <ArrowUpRight size={14} />
                    </button>
                  ))}
                </>
              )}
            </section>
          )}
          {view === "contact" && (
            <ContactView
              data={contact}
              scope={scope}
              onRefresh={() => run(loadContact)}
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
          onClose={() => setImporting(false)}
          onSubmit={async (data, file) => {
            const result = await run(async () => {
              if (file) await api.upload(file, data, file.name);
              else await api.call("receive_source", { body: data as any });
              await refresh();
              return true;
            });
            if (result) {
              setImporting(false);
              setNotice("来源已可靠接收");
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
              <Badge value={selected.kind} />
              <Badge value={selected.status} />
              <span className="label-muted">r{selected.revision}</span>
            </div>
            <h2>{selected.title || kinds[selected.kind]}</h2>
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
                      let complete: RecordItem & { content: string } = {...selected, content:selected.content ?? ""};
                      while (complete.cursor) {
                        const piece = await api.call("read_memory", {path:{record_id:complete.id},query:{offset:complete.cursor}});
                        if (piece.revision !== selected.revision) throw new Error("记忆已更新，请重新打开后纠正。");
                        complete = {...piece,content:complete.content + piece.content};
                      }
                      setSelected(complete);
                      setRevisionText(complete.content);
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
                  <dd>{selected.scope.world}</dd>
                  <dt>确认程度</dt>
                  <dd>{selected.confirmation}</dd>
                  <dt>生成内容</dt>
                  <dd>{selected.generated ? "是" : "否"}</dd>
                  <dt>独立来源</dt>
                  <dd>
                    {selected.independent_sources ?? selected.source_ids.length}
                  </dd>
                  <dt>位置</dt>
                  <dd>{JSON.stringify(selected.locator)}</dd>
                </dl>
                {selected.cursor && (
                  <button
                    className="subtle"
                    onClick={() =>
                      void run(async () => {
                        const more = await api.call("read_memory", {
                          path: { record_id: selected.id },
                          query: { offset: selected.cursor },
                        });
                        setSelected({
                          ...more,
                          content: selected.content + more.content,
                        });
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
                {selected.source_ids.map((sid) => (
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
function VirtualRecords({
  items,
  onRead,
}: {
  items: RecordItem[];
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
    return (
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
function JobList({
  jobs,
  onAction,
}: {
  jobs: any[];
  onAction: (id: string, action: string) => void;
}) {
  if (!jobs.length)
    return (
      <Empty
        title="处理队列为空"
        detail="新的解析、抽取与整理任务会显示在这里。"
      />
    );
  return (
    <div className="job-list">
      <div className="job-head">
        <span>任务</span>
        <span>状态</span>
        <span>尝试次数</span>
        <span>操作</span>
      </div>
      {jobs.slice(0, 8).map((j) => (
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
              <button
                className="text-button"
                onClick={() => onAction(j.id, "retry")}
              >
                重试
              </button>
            )}
            {["pending", "running"].includes(j.state) && (
              <button
                className="text-button"
                onClick={() => onAction(j.id, "cancel")}
              >
                取消
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
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
  onClose,
  onSubmit,
}: {
  scope: Scope;
  onClose: () => void;
  onSubmit: (data: any, file: File | null) => Promise<void>;
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
                await api.call("import_url", {
                  body: {
                    url: webUrl,
                    scope,
                    title: String(f.get("title") || ""),
                  },
                });
                onClose();
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
function ContactView({ data, scope, onRefresh, run, notice }: any) {
  const [editing, setEditing] = useState(false);
  return (
    <>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>联系策略</h2>
            <p>每个角色独立设置渠道、安静时段和发送确认。</p>
          </div>
          <button className="subtle" onClick={() => setEditing(!editing)}>
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
        {editing && (
          <form
            className="settings-form"
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              void run(async () => {
                await api.call("configure_contact", {
                  body: {
                    id: String(f.get("id")),
                    scope,
                    enabled: f.get("enabled") === "on",
                    channel: String(f.get("channel") || "") || null,
                    timezone: String(f.get("timezone")),
                    quiet_start: Number(f.get("start")),
                    quiet_end: Number(f.get("end")),
                    max_per_day: Number(f.get("max")),
                    min_interval_minutes: Number(f.get("interval")),
                    triggers: f.getAll("triggers") as any,
                    allowed_kinds: f.getAll("allowed_kinds") as any,
                    require_confirmation: f.get("confirm") === "on",
                    idempotent_channel: f.get("idempotent") === "on",
                    greeting_text:String(f.get("greeting_text")||"想聊聊今天的近况吗？"),
                  },
                });
                await onRefresh();
                setEditing(false);
              });
            }}
          >
            <label>自主问候内容<input name="greeting_text" defaultValue="想聊聊今天的近况吗？" maxLength={2000}/></label>
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
                      defaultChecked={["reminder", "commitment"].includes(k)}
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
                      defaultChecked={["reminder", "commitment"].includes(k)}
                    />
                    {String(v)}
                  </label>
                ))}
              </div>
            </fieldset>
            <label>
              最短间隔（分钟）
              <input name="interval" type="number" min="0" defaultValue="60" />
            </label>
            <div className="form-grid">
              <label>
                策略名称
                <input name="id" defaultValue="default" required />
              </label>
              <label>
                回调地址
                <input
                  name="channel"
                  type="url"
                  placeholder="https://your-host/callback"
                />
              </label>
              <label>
                时区
                <input name="timezone" defaultValue="Asia/Singapore" required />
              </label>
              <label>
                每日上限
                <input
                  name="max"
                  type="number"
                  defaultValue="3"
                  min="0"
                  max="100"
                />
              </label>
              <label>
                安静时段开始
                <input
                  name="start"
                  type="number"
                  defaultValue="22"
                  min="0"
                  max="23"
                />
              </label>
              <label>
                安静时段结束
                <input
                  name="end"
                  type="number"
                  defaultValue="8"
                  min="0"
                  max="23"
                />
              </label>
            </div>
            <label className="checkbox">
              <input type="checkbox" name="enabled" />
              启用发送
            </label>
            <label className="checkbox">
              <input type="checkbox" name="confirm" defaultChecked />
              发送前逐次确认
            </label>
            <label className="checkbox">
              <input type="checkbox" name="idempotent" />
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
            <input name="policy" defaultValue="default" />
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
                config: any = {
                  ...current,
                  protocol: String(f.get("protocol")),
                  endpoint: String(f.get("endpoint")),
                  model: String(f.get("model")),
                  api_key_env: String(f.get("env") || "") || null,
                  timeout_seconds: Number(f.get("timeout")),
                };
              if (f.get("dimensions"))
                config.dimensions = Number(f.get("dimensions"));
              void run(async () => {
                const updated = await api.call("configure_models", {
                  body: { ...models, [role]: config },
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
