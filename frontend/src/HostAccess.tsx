import { useEffect, useState } from "react";
import { api } from "./api";

export function HostAccess() {
  const [info, setInfo] = useState<any>(null);
  const [sessions, setSessions] = useState<any[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => Promise.all([api("/admin/client-access"), api<any[]>("/admin/client-sessions")])
      .then(([access, rows]) => { setInfo(access); setSessions(rows); setError(""); })
      .catch((e) => setError(e.message));
    void load(); const timer = window.setInterval(load, 30000);
    return () => clearInterval(timer);
  }, []);
  return <section className="v7-settings">
    <h3>分享客戶端連線</h3>
    <p>這台電腦同時是彤靈主機與完整工作台。其他人透過下列網址使用同一套 Agent 與手動流程；每個瀏覽器只會看見自己的專案，主機設定由你管理。</p>
    {info?.url ? <div className="card"><strong>客戶端網址</strong><p><a href={info.url} target="_blank" rel="noreferrer">{info.url}</a></p>
      <button onClick={() => navigator.clipboard.writeText(info.url)}>複製網址</button></div>
      : <p className="feedback warning">客戶端連線尚未啟動。請先在這台電腦設定 ngrok 帳戶，再執行「啟動客戶端連線」腳本。</p>}
    <div className="card"><strong>配對碼</strong><p className="share-code">{info?.code || "讀取中…"}</p>
      <p>每 10 分鐘更新；提供給你信任的使用者。配對紀錄保存在這台電腦，有效 30 天，服務重啟不需重新配對。</p>
      <button onClick={() => api("/admin/client-access").then(setInfo).catch((e) => setError(e.message))}>更新配對碼</button>
    </div>
    <div className="card"><h4>已配對的瀏覽器</h4>
      {sessions.filter((s) => !s.revoked && s.expires_at * 1000 > Date.now()).length === 0 && <p>目前沒有有效配對。</p>}
      {sessions.filter((s) => !s.revoked && s.expires_at * 1000 > Date.now()).map((s) => <div className="client-session-row" key={s.id}>
        <span>裝置 {s.client_label} · 配對於 {new Date(s.created_at).toLocaleString("zh-TW")} · 到期 {new Date(s.expires_at * 1000).toLocaleDateString("zh-TW")}</span>
        <button onClick={async () => { try { await api(`/admin/client-sessions/${s.id}/revoke`, "POST"); setSessions(await api<any[]>("/admin/client-sessions")); } catch (e) { setError((e as Error).message); } }}>中止存取</button>
      </div>)}
    </div>
    <p className="muted">共用知識只由本機管理，遠端 Agent 可檢索其中的文件；請只加入允許共享的資料。模型與系統設定只在本機工作台開放。正式 PRD 仍由使用者審閱後定版。</p>
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </section>;
}
