import { useState } from "react";
import { api } from "./api";

/** The gateway shares the project workspace after pairing; only entry differs. */
export function ClientApp({ mode, demoReason, onPaired }: { mode?: string; demoReason?: string; onPaired: () => void }) {
  const [pairCode, setPairCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function pair() {
    setBusy(true);
    setError("");
    try {
      await api("/pair", "POST", { code: pairCode.trim().toUpperCase() });
      onPaired();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return <div className="client-shell"><main className="client-card">
    <h1>連接彤靈談心</h1>
    {mode === "demo" && demoReason
      ? <p role="alert" className="feedback warning">{demoReason}</p>
      : <p>請向主機管理者取得短期配對碼。連接後可使用 Agent 或手動流程，你的專案與其他人的專案分開保存。</p>}
    <label>配對碼<input autoFocus value={pairCode} onChange={(e) => setPairCode(e.target.value.toUpperCase())} onKeyDown={(e) => { if (e.key === "Enter" && pairCode.trim() && !busy) void pair(); }} /></label>
    <button className="primary" disabled={!pairCode.trim() || busy} onClick={() => void pair()}>{busy ? "正在配對…" : "連接工作台"}</button>
    {busy && <p role="status" className="execution-card">正在驗證配對碼，請稍候。</p>}
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </main></div>;
}
