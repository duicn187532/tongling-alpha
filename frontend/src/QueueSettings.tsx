import { useEffect, useState } from "react";
import { api } from "./api";

type QueueConfig = {
  model_concurrency: number; prd_concurrency: number; retrieval_concurrency: number;
  agent_concurrency: number; queue_capacity: number; per_user_jobs: number;
  job_timeout_seconds: number; max_retries: number;
};

type ActiveJob = { job_id: string; kind: string; status: string; session: string; progress: number };

export function QueueSettings({ onSaved }: { onSaved?: (value: QueueConfig) => void }) {
  const [config, setConfig] = useState<QueueConfig | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    api<QueueConfig>("/admin/queue/settings").then((value) => { if (active) setConfig(value); }).catch((error) => { if (active) setFeedback(error.message); });
    const refresh = () => api("/admin/queue/status").then((value) => { if (active) setStatus(value); }).catch((error) => { if (active) setFeedback(error.message); });
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const fields: { key: keyof QueueConfig; label: string; min: number; max: number }[] = [
    { key: "model_concurrency", label: "同時模型推論", min: 1, max: 4 },
    { key: "agent_concurrency", label: "同時 Agent 工作", min: 1, max: 8 },
    { key: "prd_concurrency", label: "同時 PRD 工作", min: 1, max: 3 },
    { key: "retrieval_concurrency", label: "同時檢索工作", min: 1, max: 8 },
    { key: "queue_capacity", label: "等待佇列上限", min: 1, max: 200 },
    { key: "per_user_jobs", label: "每人待處理工作上限", min: 1, max: 10 },
    { key: "job_timeout_seconds", label: "等待／執行逾時（秒）", min: 30, max: 3600 },
    { key: "max_retries", label: "可安全重試次數", min: 0, max: 3 },
  ];

  return <section className="queue-settings">
    <span className="eyebrow">SERVER CAPACITY</span>
    <h2>展示併發與工作佇列</h2>
    <p>這些限制由主機統一執行；模型名額只在實際推論期間占用。變更後的新工作立即套用。</p>
    {status && <div className="queue-metrics" aria-label="佇列監控">
      <div><strong>{status.active_sessions}</strong><span>近 5 分鐘客戶端</span></div>
      <div><strong>{status.jobs?.RUNNING ?? 0}</strong><span>執行中</span></div>
      <div><strong>{status.jobs?.QUEUED ?? 0}</strong><span>等待中</span></div>
      <div><strong>{status.errors_last_24h}</strong><span>近 24 小時失敗</span></div>
      <div><strong>{status.llm_active}</strong><span>模型名額使用中</span></div>
    </div>}
    <p className="muted">GPU：{status?.gpu?.status === "AVAILABLE"
      ? `記憶體 ${status.gpu.memory_used_mb}/${status.gpu.memory_total_mb} MB · 使用率 ${status.gpu.utilization_percent}%`
      : "此主機未提供可讀取的 GPU 指標"}</p>
    {status && <div className="queue-active"><h3>目前工作</h3>
      {status.active_jobs?.length ? <ul>{(status.active_jobs as ActiveJob[]).map((job) =>
        <li key={job.job_id}><strong>{job.kind}</strong><span>{job.status === "QUEUED" ? "等待" : `執行中 ${job.progress}%`}</span><small>Session {job.session}</small></li>)}</ul>
        : <p className="muted">目前沒有等待或執行中的工作。</p>}</div>}
    {config ? <><div className="queue-fields">{fields.map(({ key, label, min, max }) =>
      <label key={key}>{label}<input type="number" min={min} max={max} value={config[key]}
        onChange={(event) => setConfig({ ...config, [key]: Number(event.target.value) })} /></label>)}</div>
      <button className="primary" disabled={busy} onClick={async () => {
        setBusy(true); setFeedback("");
        try { const saved = await api<QueueConfig>("/admin/queue/settings", "PUT", config); setConfig(saved); onSaved?.(saved); setFeedback("併發設定已保存。"); }
        catch (error) { setFeedback((error as Error).message); }
        finally { setBusy(false); }
      }}>保存展示設定</button></> : <p role="status">正在讀取設定…</p>}
    {feedback && <p role="status" className="feedback">{feedback}</p>}
  </section>;
}
