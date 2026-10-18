import { useEffect, useState } from "react";
import { api } from "./api";

const modes = [
  ["LOCAL_ONLY", "僅本機：專案文件＋本機知識"],
  ["SERVER_ONLY", "伺服器：專案文件＋共用知識"],
  ["HYBRID", "混合：專案＋本機＋伺服器"],
  ["CUSTOM", "自訂來源"],
];
const scopes = [
  ["PROJECT_LOCAL", "專案文件"],
  ["LOCAL_PRIVATE", "本機私人知識"],
  ["SERVER_SHARED", "伺服器共用知識"],
];
const emptyServer = {
  name: "", base_url: "", enabled: false, auth_env_var: "",
  timeout_seconds: 10, tls_verify: true, cache_policy: "METADATA_ONLY",
};

export function KnowledgeSettings({ project, act }: { project: any; act: (f: () => Promise<unknown>, m?: string) => Promise<void> }) {
  const [workspace, setWorkspace] = useState<any>(null);
  const [projectPolicy, setProjectPolicy] = useState<any>(null);
  const [servers, setServers] = useState<any[]>([]);
  const [serverForm, setServerForm] = useState<any>(emptyServer);
  const [selected, setSelected] = useState("");
  const [testStatus, setTestStatus] = useState<any>(null);
  const [message, setMessage] = useState("");
  async function refresh() {
    setWorkspace(await api("/settings/knowledge"));
    setServers(await api("/knowledge/servers"));
    if (project?.id) setProjectPolicy(await api(`/projects/${project.id}/knowledge-policy`));
  }
  useEffect(() => { refresh().catch((e) => setMessage(e.message)); }, [project?.id]);
  const editServer = (s: any) => { setSelected(s.id || ""); setServerForm({ ...emptyServer, ...s }); setTestStatus(null); };
  const policyFields = (value: any, change: (next: any) => void) => <>
    <label>知識模式
      <select value={value.mode} onChange={(e) => change({ ...value, mode: e.target.value })}>
        {modes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
    </label>
    {value.mode === "CUSTOM" && <div className="actions">{scopes.map(([key, label]) =>
      <label key={key}><input type="checkbox" checked={value.scopes?.includes(key) || false}
        onChange={(e) => change({ ...value, scopes: e.target.checked ? [...(value.scopes || []), key] : value.scopes.filter((s: string) => s !== key) })} />{label}</label>)}</div>}
    <label><input type="checkbox" checked={value.server_enabled} onChange={(e) => change({ ...value, server_enabled: e.target.checked })} />允許此範圍使用知識伺服器</label>
    <label><input type="checkbox" checked={value.allow_remote_query} onChange={(e) => change({ ...value, allow_remote_query: e.target.checked })} />允許將搜尋詞送到知識伺服器</label>
    <label><input type="checkbox" checked={value.external_research} onChange={(e) => change({ ...value, external_research: e.target.checked })} />納入外部研究報導（與伺服器知識分開）</label>
    <p className="muted">預設不傳送專案名稱、需求全文或本機文件節錄；遠端查詢只送搜尋詞。</p>
  </>;
  return <section className="v7-settings">
    <h3>知識來源與隱私</h3>
    <p>先選工作空間預設。專案可另設來源；伺服器無法連線時會保留可用的本機結果。</p>
    {workspace && <div className="card"><h4>工作空間預設</h4>{policyFields(workspace, setWorkspace)}
      <button className="primary" onClick={() => act(async () => { setWorkspace(await api("/settings/knowledge", "PATCH", workspace)); setMessage("工作空間知識設定已保存。"); })}>保存工作空間設定</button></div>}
    {projectPolicy && <div className="card"><h4>目前專案：{project.name}</h4>
      <label><input type="checkbox" checked={projectPolicy.use_workspace_default}
        onChange={(e) => setProjectPolicy({ ...projectPolicy, use_workspace_default: e.target.checked, override: e.target.checked ? null : { ...projectPolicy.effective } })} />沿用工作空間預設</label>
      {!projectPolicy.use_workspace_default && policyFields(projectPolicy.override || projectPolicy.effective,
        (next) => setProjectPolicy({ ...projectPolicy, override: next }))}
      <button onClick={() => act(async () => { setProjectPolicy(await api(`/projects/${project.id}/knowledge-policy`, "PATCH", projectPolicy.use_workspace_default ? null : projectPolicy.override)); setMessage("專案知識設定已保存。"); })}>保存專案設定</button>
    </div>}
    <div className="card"><h4>共用知識伺服器</h4>
      <p>連線須由伺服器提供 /health、/capabilities、/search。金鑰只讀取後端環境變數，不在此頁輸入或保存。</p>
      {servers.length === 0 && <p className="feedback warning">尚未設定共用知識伺服器。選擇混合或伺服器模式後，仍須新增並測試連線，才會取得遠端來源。</p>}
      {servers.map((s) => <button key={s.id} onClick={() => editServer(s)}>{s.name} · {s.enabled ? "啟用" : "停用"}</button>)}
      <button onClick={() => editServer(emptyServer)}>新增伺服器</button>
      <div className="form-grid">
        <label>名稱<input value={serverForm.name} onChange={(e) => setServerForm({ ...serverForm, name: e.target.value })} /></label>
        <label>網址<input value={serverForm.base_url} placeholder="https://knowledge.example.com" onChange={(e) => setServerForm({ ...serverForm, base_url: e.target.value })} /></label>
        <label>金鑰環境變數名稱<input value={serverForm.auth_env_var} placeholder="TONGLING_KB_TOKEN" onChange={(e) => setServerForm({ ...serverForm, auth_env_var: e.target.value })} /></label>
        <label>逾時秒數<input type="number" min={2} max={120} value={serverForm.timeout_seconds} onChange={(e) => setServerForm({ ...serverForm, timeout_seconds: Number(e.target.value) })} /></label>
        <label>本機快取政策<select value={serverForm.cache_policy} onChange={(e) => setServerForm({ ...serverForm, cache_policy: e.target.value })}>
          {["DISABLED", "METADATA_ONLY", "SNIPPET_ONLY", "FULL_CONTENT"].map((x) => <option key={x}>{x}</option>)}</select></label>
      </div>
      <label><input type="checkbox" checked={serverForm.enabled} onChange={(e) => setServerForm({ ...serverForm, enabled: e.target.checked })} />啟用</label>
      <label><input type="checkbox" checked={serverForm.tls_verify} onChange={(e) => setServerForm({ ...serverForm, tls_verify: e.target.checked })} />驗證 TLS 憑證</label>
      <div className="actions"><button className="primary" onClick={() => act(async () => { const s = await api(selected ? `/knowledge/servers/${selected}` : "/knowledge/servers", selected ? "PATCH" : "POST", serverForm); editServer(s); await refresh(); setMessage("伺服器設定已保存。"); })}>保存伺服器</button>
        {selected && <button onClick={() => act(async () => { const result = await api(`/knowledge/servers/${selected}/test`, "POST"); setTestStatus(result); await refresh(); })}>測試連線與能力</button>}</div>
      {testStatus && <p role="status" className={`feedback ${testStatus.status === "CONNECTED" ? "success" : "warning"}`}>{testStatus.status === "CONNECTED" ? `已連線：${testStatus.capabilities.join("、")}` : `連線失敗：${testStatus.detail}`}</p>}
    </div>
    {message && <p role="status" className="feedback success">{message}</p>}
  </section>;
}
