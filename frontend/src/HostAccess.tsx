import { useEffect, useState } from "react";
import { api } from "./api";

export function HostAccess() {
  const [info, setInfo] = useState<any>(null);
  const [sessions, setSessions] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const load = () => Promise.all([api("/admin/client-access"), api<any[]>("/admin/client-sessions")])
    .then(([access, rows]) => { setInfo(access); setSessions(rows); setError(""); })
    .catch((e) => setError(e.message));
  useEffect(() => {
    void load(); const timer = window.setInterval(load, 30000);
    return () => clearInterval(timer);
  }, []);
  async function saveMode(mode: string, demoNetwork = info?.demo_network || "LAN") {
    if (mode === "demo" && demoNetwork === "PUBLIC" &&
      !window.confirm("開放網際網路展示後，任何取得網址的人都能直接建立專案並使用模型額度。確定要開放？")) return;
    setSaving(true); setNotice("");
    try {
      const result = await api("/admin/client-access/mode", "PUT", { mode, demo_network: demoNetwork });
      setNotice(mode === "demo" ? "已切換為展示模式，客戶端開啟網址即可使用。"
        : `已切換為正式模式${result.revoked_demo_sessions ? `，並中止 ${result.revoked_demo_sessions} 個展示連線` : ""}；已配對的裝置不受影響。`);
      await load();
    } catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  }
  const active = sessions.filter((s) => !s.revoked && s.expires_at * 1000 > Date.now());
  const demo = info?.mode === "demo";
  return <section className="v7-settings">
    <h3>伺服器／客戶端連線</h3>
    <p>這台電腦同時是彤靈主機與完整工作台。其他人透過下列網址使用同一套 Agent 與手動流程；每個瀏覽器只會看見自己的專案，主機設定由你管理。</p>
    <div className="card">
      <strong>連線驗證模式</strong>
      <div className="access-mode-options" role="radiogroup" aria-label="連線驗證模式">
        <label><input type="radio" name="access-mode" checked={!demo} disabled={saving || !info} onChange={() => void saveMode("pairing")} /> 正式模式：需要配對碼</label>
        <label><input type="radio" name="access-mode" checked={demo} disabled={saving || !info} onChange={() => void saveMode("demo")} /> 展示模式：免配對碼直接連線</label>
      </div>
      {demo && <>
        <label>展示開放範圍
          <select value={info.demo_network} disabled={saving} onChange={(e) => void saveMode("demo", e.target.value)}>
            <option value="LAN">僅限區域網路（建議）</option>
            <option value="PUBLIC">也允許 ngrok 網際網路網址</option>
          </select>
        </label>
        <p role="note" className="feedback warning">安全提醒：展示模式不需配對碼，{info.demo_network === "PUBLIC" ? "任何取得網址的人" : "同一區域網路內的任何人"}都能建立展示連線並使用模型。每個瀏覽器仍有獨立 Session（{info.demo_session_hours} 小時到期，最多 {info.demo_max_sessions} 個），無法進入主機設定或他人專案。請勿放入真實客戶資料，展示結束請切回正式模式。</p>
      </>}
      {notice && <p role="status" className="feedback">{notice}</p>}
    </div>
    {info?.url ? <div className="card"><strong>客戶端網址</strong><p><a href={info.url} target="_blank" rel="noreferrer">{info.url}</a></p>
      <button onClick={() => navigator.clipboard.writeText(info.url)}>複製網址</button></div>
      : <p className="feedback warning">網際網路客戶端尚未啟動。區域網路展示請執行 client-lan.cmd；外部連線請先設定 ngrok，再執行 client.cmd。</p>}
    {!demo && <div className="card"><strong>配對碼</strong><p className="share-code">{info?.code || "讀取中…"}</p>
      <p>每 10 分鐘更新；提供給你信任的使用者。配對紀錄保存在這台電腦，有效 30 天，服務重啟不需重新配對。</p>
      <button onClick={() => api("/admin/client-access").then(setInfo).catch((e) => setError(e.message))}>更新配對碼</button>
    </div>}
    <div className="card"><h4>已連線的瀏覽器</h4>
      {active.length === 0 && <p>目前沒有有效連線。</p>}
      {active.map((s) => <div className="client-session-row" key={s.id}>
        <span>{s.kind === "DEMO" ? "展示" : "配對"} · 裝置 {s.client_label} · 建立於 {new Date(s.created_at).toLocaleString("zh-TW")} · 到期 {new Date(s.expires_at * 1000).toLocaleString("zh-TW")}</span>
        <button onClick={async () => { try { await api(`/admin/client-sessions/${s.id}/revoke`, "POST"); setSessions(await api<any[]>("/admin/client-sessions")); } catch (e) { setError((e as Error).message); } }}>中止存取</button>
      </div>)}
    </div>
    <p className="muted">共用知識只由本機管理，遠端 Agent 可檢索其中的文件；請只加入允許共享的資料。模型與系統設定只在本機工作台開放，Ollama 不會對客戶端開放。正式 PRD 仍由使用者審閱後定版。</p>
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </section>;
}
