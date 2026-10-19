import { useEffect, useMemo, useState } from "react";
import { api } from "./api";

type Todo = { id: string; title: string; due_date: string | null; done: boolean };
type Suggestion = { title: string; step: number; kind: string };
type Card = {
  id: string; name: string; description: string; mode: string; runtime: string;
  status: string; progress: number; milestones: boolean[]; revision: string | null;
  counts: { sources: number; evidence_sources: number; requirements: number; pending_decisions: number; pending_meetings: number; open_changes: number };
  suggestions: Suggestion[]; todos: Todo[]; active_work: string | null; updated_at: string;
};
type Snapshot = { projects: Card[]; totals: { projects: number; running: number; attention: number; baselined: number; open_todos: number } };

const statusLabels: Record<string, string> = {
  RUNNING: "AI 工作中", ATTENTION: "待 PM 處理", BASELINED: "業務版已定版",
  REVIEW: "PRD 草稿待審", PLANNING: "需求整理中",
};
const milestoneNames = ["需求起點", "證據", "需求確認", "PRD 草稿", "業務基準"];

export function ProjectHub({ onOpen, onCreate, onDemo, onSample, isClient }: {
  onOpen: (id: string, step?: number) => void;
  onCreate: () => void;
  onDemo?: () => void;
  onSample?: () => void;
  isClient: boolean;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [dates, setDates] = useState<Record<string, string>>({});
  const [pending, setPending] = useState("");
  async function refresh() {
    try { setSnapshot(await api<Snapshot>("/dashboard")); setError(""); }
    catch (cause) { setError(`無法更新專案總覽：${(cause as Error).message}`); }
  }
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 20000);
    return () => window.clearInterval(timer);
  }, []);
  async function change(key: string, action: () => Promise<unknown>) {
    setPending(key); setError("");
    try { await action(); await refresh(); }
    catch (cause) { setError((cause as Error).message); }
    finally { setPending(""); }
  }
  const visible = useMemo(() => (snapshot?.projects || [])
    .filter((project) => filter === "ALL" || project.status === filter)
    .filter((project) => project.name.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      const priority = (status: string) => ({ ATTENTION: 0, RUNNING: 1, REVIEW: 2, PLANNING: 3, BASELINED: 4 }[status] ?? 5);
      return priority(a.status) - priority(b.status) || b.updated_at.localeCompare(a.updated_at);
    }), [snapshot, filter, search]);
  const totals = snapshot?.totals;
  return <section className="hub" aria-label="彤靈專案總覽">
    <div className="hub-hero">
      <div><span className="eyebrow">TONG LING · PM WORKSPACE</span>
        <h1>每一份 PRD，都有清楚的下一步。</h1>
        <p>在這裡掌握所有需求專案、人工決策和待辦。文件、模型與公司服務可從外部接入；彤靈也能用內建知識庫在本機獨立工作。</p>
      </div>
      <div className="hub-hero-actions"><button className="primary" onClick={onCreate}>＋ 建立專案</button>
        {onSample && <button onClick={onSample}>✦ AI 產生範例需求</button>}
        {!isClient && onDemo && <button onClick={onDemo}>✦ 套用範例</button>}</div>
    </div>
    <div className="hub-metrics" aria-label="工作概況">
      <div><strong>{totals?.projects ?? "—"}</strong><span>專案總數</span></div>
      <div><strong>{totals?.attention ?? "—"}</strong><span>需要你處理</span></div>
      <div><strong>{totals?.running ?? "—"}</strong><span>AI 工作中</span></div>
      <div><strong>{totals?.open_todos ?? "—"}</strong><span>未完成個人代辦</span></div>
      <div><strong>{totals?.baselined ?? "—"}</strong><span>業務版已定版</span></div>
    </div>
    <div className="hub-toolbar"><div><h2>我的專案</h2><p>按待處理程度排序；點選卡片進入 Agent 或手動流程。</p></div>
      <div className="hub-filters"><input aria-label="搜尋專案" placeholder="搜尋專案" value={search} onChange={(event) => setSearch(event.target.value)} />
        <select aria-label="篩選狀態" value={filter} onChange={(event) => setFilter(event.target.value)}>
          <option value="ALL">全部狀態</option>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select></div>
    </div>
    {error && <p className="feedback warning" role="alert">{error}</p>}
    {!snapshot && !error && <p role="status">正在整理各專案狀態…</p>}
    {snapshot && !snapshot.projects.length && <div className="hub-empty"><h2>從第一個需求開始</h2><p>建立專案後，彤靈會在這裡整理進度與下一步。</p><button className="primary" onClick={onCreate}>建立第一個專案</button></div>}
    {snapshot && !!snapshot.projects.length && !visible.length && <p className="hub-empty">沒有符合條件的專案。</p>}
    <div className="hub-grid">{visible.map((project) => <article className="hub-project" key={project.id}>
      <div className="hub-project-top"><span className={`hub-status ${project.status.toLowerCase()}`}>{statusLabels[project.status]}</span>
        <span className="hub-mode">{project.mode === "MANUAL" ? "手動流程" : "Agent 模式"}</span></div>
      <h3>{project.name}</h3><p className="hub-description">{project.description || "尚未填寫需求起點"}</p>
      <div className="hub-progress-head"><strong>PRD 流程 {project.progress}%</strong><span>{project.revision ? `目前版本 ${project.revision}` : "尚無 PRD 版本"}</span></div>
      <progress max={100} value={project.progress} aria-label={`${project.name} 的 PRD 流程進度`} />
      <div className="hub-milestones">{milestoneNames.map((name, index) => <span className={project.milestones[index] ? "done" : ""} key={name}>{project.milestones[index] ? "✓" : "○"} {name}</span>)}</div>
      <p className="hub-counts">{project.counts.evidence_sources} 份佐證 · {project.counts.requirements} 項需求 · {project.counts.pending_decisions} 項待決策</p>
      {project.active_work && <p className="hub-running" role="status">◉ {project.active_work} 執行中</p>}
      <div className="hub-next"><strong>系統建議下一步</strong>
        {project.suggestions.length ? project.suggestions.map((item, index) => <button key={`${item.kind}-${index}`} onClick={() => onOpen(project.id, item.step)}>{item.title} <span>→</span></button>)
          : <p>目前沒有流程待辦；可檢視版本與後續變更。</p>}</div>
      <div className="hub-todos"><strong>我的代辦 · {project.todos.filter((todo) => !todo.done).length} 未完成</strong>
        {project.todos.map((todo) => <div className="hub-todo" key={todo.id}>
          <label><input type="checkbox" checked={todo.done} disabled={!!pending} onChange={(event) => void change(todo.id, () => api(`/projects/${project.id}/todos/${todo.id}`, "PATCH", { done: event.target.checked }))} />
            <span className={todo.done ? "done" : ""}>{todo.title}</span></label>
          {todo.due_date && <small>{todo.due_date}</small>}
          <button aria-label={`移除代辦：${todo.title}`} disabled={!!pending} onClick={() => {
            if (window.confirm(`移除代辦「${todo.title}」？`)) void change(todo.id, () => api(`/projects/${project.id}/todos/${todo.id}`, "DELETE"));
          }}>×</button>
        </div>)}
        <form className="hub-add-todo" onSubmit={(event) => { event.preventDefault(); const title = (drafts[project.id] || "").trim(); if (!title) return;
          void change(project.id, async () => { await api(`/projects/${project.id}/todos`, "POST", { title, due_date: dates[project.id] || null });
            setDrafts((current) => ({ ...current, [project.id]: "" })); setDates((current) => ({ ...current, [project.id]: "" })); }); }}>
          <input aria-label={`新增 ${project.name} 的代辦`} placeholder="新增自己的代辦" maxLength={180} value={drafts[project.id] || ""} onChange={(event) => setDrafts((current) => ({ ...current, [project.id]: event.target.value }))} />
          <input aria-label={`設定 ${project.name} 代辦日期`} type="date" value={dates[project.id] || ""} onChange={(event) => setDates((current) => ({ ...current, [project.id]: event.target.value }))} />
          <button disabled={!!pending || !(drafts[project.id] || "").trim()} type="submit">加入</button>
        </form>
      </div>
      <button className="hub-open" onClick={() => onOpen(project.id)}>開啟專案工作台 →</button>
    </article>)}</div>
  </section>;
}
