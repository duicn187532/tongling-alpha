import { useEffect, useState } from "react";

const names: Record<string, string> = {
  QUEUED: "排隊中", RUNNING: "執行中", SUCCEEDED: "已完成",
  WAITING: "等待你處理", FAILED: "執行失敗", CANCELLED: "已取消",
  INTERRUPTED: "執行已中斷",
};

export function ActivityCards({ tasks }: { tasks: any[] }) {
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
  const primary = active[0] || recent[0];
  if (!primary) return null;
  const running = primary.status === "RUNNING";
  const heartbeatAge = Math.max(0, Math.floor((clock - Date.parse(primary.heartbeat_at || primary.created_at)) / 1000));
  const phaseAge = Math.max(0, Math.floor((clock - Date.parse(primary.updated_at || primary.created_at)) / 1000));
  const seconds = Math.max(0, Math.floor(((primary.finished_at ? Date.parse(primary.finished_at) : clock) - Date.parse(primary.created_at)) / 1000));
  return <section className="execution-list" aria-label="AI 執行狀態">
    <article className={`execution-card ${primary.status.toLowerCase()}`} data-testid="execution-card">
      <header><strong>{primary.title}</strong><span className="badge">{names[primary.status] || primary.status}</span></header>
      <p className="execution-phase" aria-live="polite">{running && <span className="spinner" />}{running || primary.status === "QUEUED" ? primary.phase : names[primary.status]}</p>
      <p>{primary.detail}</p>
      <small>{primary.model || "本機處理"} · {primary.finished_at ? "耗時" : "已等待"} {seconds} 秒</small>
      {running && <p className={heartbeatAge > 15 ? "execution-warning" : "muted"}>
        {heartbeatAge > 15 ? `已 ${heartbeatAge} 秒未收到工作心跳，無法確認仍在執行。` : `工作心跳：${heartbeatAge} 秒前；本階段 ${phaseAge} 秒。`}
      </p>}
      {running && phaseAge >= 60 && <p className="execution-warning">此階段耗時較久，請耐心等候；若心跳中斷可刷新狀態後重試。</p>}
      {(active.length > 1 || recent.length > (active.length ? 0 : 1)) && <details className="activity-history">
        <summary>{active.length > 1 ? `另有 ${active.length - 1} 項執行中 · ` : ""}查看最近工作紀錄</summary>
        {[...active.slice(1), ...recent.filter((task) => task.id !== primary.id)].map((task) =>
          <div className="activity-history-row" key={task.id}><span>{task.title}</span><small>{names[task.status] || task.status} · {task.phase}</small></div>)}
      </details>}
    </article>
  </section>;
}
