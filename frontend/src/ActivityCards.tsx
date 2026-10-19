import { useEffect, useState } from "react";

const names: Record<string, string> = {
  QUEUED: "排隊中", RUNNING: "執行中", SUCCEEDED: "已完成",
  WAITING: "等待你處理", FAILED: "執行失敗", CANCELLED: "已取消",
  INTERRUPTED: "執行已中斷",
};
// Success and cancellation are notices that fade; failures wait for the user.
const FADE_SECONDS: Record<string, number> = { SUCCEEDED: 120, CANCELLED: 60 };
const ACTIONABLE = new Set(["FAILED", "INTERRUPTED", "WAITING"]);

export function noticeVisible(status: string, finishedAt: string | null | undefined, dismissedAt: string | null | undefined, now: number) {
  if (dismissedAt) return false;
  if (["RUNNING", "QUEUED"].includes(status)) return true;
  const fade = FADE_SECONDS[status];
  if (fade === undefined) return true;
  return !!finishedAt && now - Date.parse(finishedAt) < fade * 1000;
}

export function ActivityCards({ tasks, onDismiss }: { tasks: any[]; onDismiss?: (id: string) => void }) {
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const ordered = [...new Map(tasks.filter(Boolean).map((task) => [task.id, task])).values()]
    .filter((task) => !(task.title === "Agent 工作流程" && task.status === "SUCCEEDED" &&
      task.finished_at && Date.parse(task.finished_at) - Date.parse(task.created_at) < 1000))
    .sort((a, b) => Date.parse(b.created_at || "") - Date.parse(a.created_at || ""));
  const active = ordered.filter((task) => ["RUNNING", "QUEUED"].includes(task.status));
  const recent = ordered.filter((task) => !["RUNNING", "QUEUED"].includes(task.status)).slice(0, 5);
  // Only the newest finished task can be current; older waits and errors are superseded.
  const latestFinished = recent[0];
  const primary = active[0] || (latestFinished && noticeVisible(latestFinished.status, latestFinished.finished_at,
    latestFinished.dismissed_at, clock) ? latestFinished : undefined);
  const history = [...active.slice(1), ...recent.filter((task) => task.id !== primary?.id)];
  if (!primary && !history.length) return null;
  if (!primary) return <section className="execution-list" aria-label="AI 執行狀態">
    <details className="activity-history"><summary>最近工作紀錄 · {history.length}</summary>
      {history.map((task) => <div className="activity-history-row" key={task.id}><span>{task.title}</span><small>{names[task.status] || task.status} · {task.phase}</small></div>)}
    </details>
  </section>;
  const running = primary.status === "RUNNING";
  const heartbeatAge = Math.max(0, Math.floor((clock - Date.parse(primary.heartbeat_at || primary.created_at)) / 1000));
  const phaseAge = Math.max(0, Math.floor((clock - Date.parse(primary.updated_at || primary.created_at)) / 1000));
  const seconds = Math.max(0, Math.floor(((primary.finished_at ? Date.parse(primary.finished_at) : clock) - Date.parse(primary.created_at)) / 1000));
  const finished = !["RUNNING", "QUEUED"].includes(primary.status);
  return <section className="execution-list" aria-label="AI 執行狀態">
    <article className={`execution-card ${primary.status.toLowerCase()}`} data-testid="execution-card">
      <header><strong>{primary.title}</strong><span className="badge">{names[primary.status] || primary.status}</span>
        {finished && onDismiss && <button className="text-button notice-dismiss" aria-label={`清除通知：${primary.title}`} onClick={() => onDismiss(primary.id)}>知道了 ✕</button>}</header>
      <p className="execution-phase" aria-live="polite">{running && <span className="spinner" />}{running || primary.status === "QUEUED" ? primary.phase : names[primary.status]}</p>
      <p>{primary.detail}</p>
      <small>{primary.model || "本機處理"} · {primary.finished_at ? "耗時" : "已等待"} {seconds} 秒{finished && !ACTIONABLE.has(primary.status) ? " · 稍後自動收起" : ""}</small>
      {running && heartbeatAge > 30 && <p className="execution-warning">已 {heartbeatAge} 秒沒有收到進度，可能已中斷；可按「更新狀態」確認，必要時重試。</p>}
      {running && heartbeatAge <= 30 && phaseAge >= 90 && <p className="muted">本地模型處理較久（此階段 {Math.floor(phaseAge / 60)} 分鐘），仍在進行中。</p>}
      {history.length > 0 && <details className="activity-history">
        <summary>{active.length > 1 ? `另有 ${active.length - 1} 項執行中 · ` : ""}查看最近工作紀錄</summary>
        {history.map((task) =>
          <div className="activity-history-row" key={task.id}><span>{task.title}</span><small>{names[task.status] || task.status} · {task.phase}</small></div>)}
      </details>}
    </article>
  </section>;
}
