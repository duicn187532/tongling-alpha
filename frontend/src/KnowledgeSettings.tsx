import { useEffect, useState } from "react";
import { api } from "./api";

const DEMO_URL = "http://127.0.0.1:8765";
const modes = [
  ["LOCAL_ONLY", "只使用彤靈內建知識庫"],
  ["SERVER_ONLY", "專案文件＋公司檔案服務器"],
  ["HYBRID", "混合：內建知識庫＋公司檔案服務器"],
  ["CUSTOM", "自訂來源"],
];
const scopes = [
  ["PROJECT_LOCAL", "專案文件"],
  ["LOCAL_PRIVATE", "彤靈內建知識庫"],
  ["SERVER_SHARED", "公司檔案服務器"],
];
const emptyServer = {
  name: "", base_url: "", enabled: false, auth_env_var: "",
  timeout_seconds: 10, tls_verify: true, cache_policy: "SNIPPET_ONLY",
};

type Props = {
  project: any;
  act: (f: () => Promise<unknown>, m?: string) => Promise<void>;
  onOpenLocal: () => void;
};

export function KnowledgeSettings({ project, act, onOpenLocal }: Props) {
  const [workspace, setWorkspace] = useState<any>(null);
  const [projectPolicy, setProjectPolicy] = useState<any>(null);
  const [servers, setServers] = useState<any[]>([]);
  const [localCount, setLocalCount] = useState(0);
  const [serverForm, setServerForm] = useState<any>(emptyServer);
  const [selected, setSelected] = useState("");
  const [serverDocs, setServerDocs] = useState<any[]>([]);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [fileFilter, setFileFilter] = useState("ALL");
  const [fileTerm, setFileTerm] = useState("");
  const [fileLimit, setFileLimit] = useState(30);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<any[]>([]);
  const [testStatus, setTestStatus] = useState<any>(null);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const demoSelected = servers.some((server) => server.id === selected && server.base_url === DEMO_URL);
  const visibleFiles = serverDocs.filter((doc) => (fileFilter === "ALL" || doc.category === fileFilter) &&
    (doc.title || "").toLowerCase().includes(fileTerm.toLowerCase()));

  async function refresh() {
    const [policy, savedServers, localDocs] = await Promise.all([
      api("/settings/knowledge"), api<any[]>("/knowledge/servers"),
      api<any[]>("/knowledge/documents"),
    ]);
    setWorkspace(policy); setServers(savedServers); setLocalCount(localDocs.filter((d) => d.searchable).length);
    if (project?.id) setProjectPolicy(await api(`/projects/${project.id}/knowledge-policy`));
  }
  useEffect(() => { refresh().catch((error) => setMessage(error.message)); }, [project?.id]);
  useEffect(() => {
    if (selected || !servers.length) return;
    const server = servers.find((item) => item.enabled) || servers[0];
    editServer(server);
    api(`/knowledge/servers/${server.id}/test`, "POST").then((result) => {
      setTestStatus(result);
      if (result.status === "CONNECTED") return loadServerDocuments(server.id);
    }).catch((error) => setMessage(`連線檢查失敗：${error.message}`));
  }, [servers, selected]);
  async function run(label: string, action: () => Promise<void>) {
    setPending(label); setMessage("");
    try { await act(action, label); } finally { setPending(""); }
  }
  function editServer(server: any) {
    setSelected(server.id || ""); setServerForm({ ...emptyServer, ...server });
    setServerDocs([]); setServerErrors({}); setPreview([]); setTestStatus(null);
    setFileFilter("ALL"); setFileTerm(""); setFileLimit(30);
  }
  async function loadServerDocuments(id: string) {
    try {
      const response = await api<{ documents: any[]; errors: Record<string, string> }>(`/knowledge/servers/${id}/documents`);
      setServerDocs(response.documents || []); setServerErrors(response.errors || {});
      setFileLimit(30);
    } catch (error) {
      setServerDocs([]); setMessage(`已連線，但此服務未提供文件清單：${(error as Error).message}`);
    }
  }
  const policyFields = (value: any, change: (next: any) => void) => <>
    <label>檢索來源
      <select value={value.mode} onChange={(event) => change({ ...value, mode: event.target.value })}>
        {modes.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
    </label>
    {value.mode === "CUSTOM" && <div className="actions">{scopes.map(([key, label]) =>
      <label key={key}><input type="checkbox" checked={value.scopes?.includes(key) || false}
        onChange={(event) => change({ ...value, scopes: event.target.checked ? [...(value.scopes || []), key] : value.scopes.filter((scope: string) => scope !== key) })} />{label}</label>)}</div>}
    <label><input type="checkbox" checked={value.server_enabled} onChange={(event) => change({ ...value, server_enabled: event.target.checked })} />啟用公司檔案服務器來源</label>
    <label><input type="checkbox" checked={value.allow_remote_query} onChange={(event) => change({ ...value, allow_remote_query: event.target.checked })} />允許將搜尋詞送至公司檔案服務器</label>
    <label><input type="checkbox" checked={value.external_research} onChange={(event) => change({ ...value, external_research: event.target.checked })} />納入外部研究報導</label>
    <p className="muted">只送搜尋詞，不傳專案名稱、需求全文或內建文件內容。公司伺服器斷線時，彤靈仍使用可用的內建知識。</p>
  </>;

  return <section className="v7-settings">
    <h3>知識來源</h3>
    <p>兩個來源各自管理。彤靈內建知識庫保存在本機；公司檔案服務器是獨立程序，透過 API 連線，不會把整個資料夾匯入彤靈。</p>

    <div className="card"><h4>① 彤靈內建知識庫</h4>
      <p><strong>{localCount}</strong> 份可檢索文件 · 文件、標籤、圖譜與索引都由彤靈在本機管理。</p>
      <div className="actions"><button onClick={onOpenLocal}>管理內建文件與圖譜</button></div>
    </div>

    <div className="card"><h4>② 公司檔案服務器</h4>
      <p>本機模擬服務：<code>{DEMO_URL}</code>；檔案放在程式資料夾的 <code>company_files</code>。正式公司服務可改填其 API 網址與金鑰環境變數。</p>
      <div className="actions"><button className="primary" disabled={!!pending} onClick={() => run("連接本機公司檔案服務器", async () => {
        const existing = servers.find((server) => server.base_url === DEMO_URL);
        const body = { ...emptyServer, name: "本機公司檔案服務器（模擬）", base_url: DEMO_URL, enabled: true };
        const server = await api(existing ? `/knowledge/servers/${existing.id}` : "/knowledge/servers", existing ? "PATCH" : "POST", body);
        editServer(server);
        const result = await api(`/knowledge/servers/${server.id}/test`, "POST");
        setTestStatus(result);
        if (result.status !== "CONNECTED") { setMessage(`服務器已保存，但連線失敗：${result.detail}`); await refresh(); return; }
        const updated = await api("/settings/knowledge", "PATCH", { ...workspace, mode: "HYBRID", server_enabled: true, allow_remote_query: true });
        setWorkspace(updated); await refresh(); await loadServerDocuments(server.id);
        setMessage("已連接公司檔案服務器並啟用混合檢索；原有內建文件仍保留。");
      })}>連接本機模擬服務並啟用混合檢索</button>
        <button disabled={!!pending} onClick={() => { editServer(emptyServer); setAdvancedOpen(true); }}>設定其他公司 API</button></div>
      {servers.length > 0 && <div className="actions">{servers.map((server) => <button key={server.id} onClick={() => { editServer(server); setAdvancedOpen(false); }}>
        {server.name} · {server.enabled ? "已啟用" : "未啟用"}</button>)}</div>}
      <details open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}><summary>API 連線設定</summary>
        <div className="form-grid">
          <label>名稱<input value={serverForm.name} onChange={(event) => setServerForm({ ...serverForm, name: event.target.value })} /></label>
          <label>API 網址<input value={serverForm.base_url} placeholder={DEMO_URL} onChange={(event) => setServerForm({ ...serverForm, base_url: event.target.value })} /></label>
          <label>金鑰環境變數名稱<input value={serverForm.auth_env_var} placeholder="COMPANY_KB_TOKEN（本機模擬免填）" onChange={(event) => setServerForm({ ...serverForm, auth_env_var: event.target.value })} /></label>
          <label>逾時秒數<input type="number" min={2} max={120} value={serverForm.timeout_seconds} onChange={(event) => setServerForm({ ...serverForm, timeout_seconds: Number(event.target.value) })} /></label>
          <label>本機保留範圍<select value={serverForm.cache_policy} onChange={(event) => setServerForm({ ...serverForm, cache_policy: event.target.value })}>
            {[ ["DISABLED", "不保留內容"], ["METADATA_ONLY", "只保留中繼資料"], ["SNIPPET_ONLY", "搜尋片段"], ["FULL_CONTENT", "完整內容"] ].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        </div>
        <label><input type="checkbox" checked={serverForm.enabled} onChange={(event) => setServerForm({ ...serverForm, enabled: event.target.checked })} />啟用此服務器</label>
        <label><input type="checkbox" checked={serverForm.tls_verify} onChange={(event) => setServerForm({ ...serverForm, tls_verify: event.target.checked })} />驗證 HTTPS 憑證</label>
        <div className="actions"><button disabled={!!pending || !serverForm.name.trim() || !serverForm.base_url.trim()} onClick={() => run("保存 API 連線", async () => {
          const server = await api(selected ? `/knowledge/servers/${selected}` : "/knowledge/servers", selected ? "PATCH" : "POST", serverForm);
          editServer(server); await refresh(); setMessage("API 連線設定已保存，請測試連線。");
        })}>保存設定</button>
          {selected && <button disabled={!!pending} onClick={() => run("測試公司伺服器", async () => {
            const result = await api(`/knowledge/servers/${selected}/test`, "POST");
            setTestStatus(result); if (result.status === "CONNECTED") await loadServerDocuments(selected);
            await refresh();
          })}>測試連線並載入文件</button>}</div>
      </details>
      {testStatus && <p role="status" className={`feedback ${testStatus.status === "CONNECTED" ? "success" : "warning"}`}>
        {testStatus.status === "CONNECTED" ? `連線正常 · ${testStatus.health?.documents ?? "—"} 份文件 · ${testStatus.capabilities.join("、")}` : `連線失敗：${testStatus.detail}`}</p>}
      {selected && testStatus?.status === "CONNECTED" && <>
        <div className="actions"><button disabled={!!pending} onClick={() => run("重讀公司文件", async () => {
          await api(`/knowledge/servers/${selected}/rescan`, "POST"); await loadServerDocuments(selected);
          setMessage("公司檔案服務器已重新掃描資料夾。");
        })}>重新掃描公司資料夾</button>
          <input aria-label="搜尋公司文件" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="試查公司文件關鍵字" />
          <button disabled={!!pending || !query.trim()} onClick={() => run("試查公司文件", async () => {
            const result = await api(`/knowledge/servers/${selected}/preview`, "POST", { query, top_k: 5 });
            setPreview(result.results || []); setMessage(`公司文件試查完成：${result.results?.length || 0} 筆。`);
          })}>試查</button></div>
        <p>服務器文件 {serverDocs.length} 份；搜尋時彤靈透過 API 取回相關片段，不會整批複製。</p>
        <div className="actions"><input aria-label="篩選公司檔名" placeholder="篩選檔名" value={fileTerm} onChange={(event) => { setFileTerm(event.target.value); setFileLimit(30); }} />
          <select aria-label="篩選公司檔案類型" value={fileFilter} onChange={(event) => { setFileFilter(event.target.value); setFileLimit(30); }}>
            <option value="ALL">全部檔案</option><option value="DOCUMENT">文件</option><option value="GRAPH">圖譜／圖表</option><option value="IMAGE">圖片</option><option value="FILE">其他附件</option>
          </select><small>符合條件 {visibleFiles.length} 份</small></div>
        {Object.entries(serverErrors).map(([name, error]) => <p className="feedback warning" key={name}>{name}：{error}</p>)}
        <div className="kb-document-list">{visibleFiles.slice(0, fileLimit).map((doc) => <article className="company-file" key={doc.id}>
          <div><strong>{doc.title}</strong><small>{({ IMAGE: "圖片", GRAPH: "圖譜／圖表", DOCUMENT: "文件", FILE: "其他附件" } as Record<string, string>)[doc.category] || "附件"} · {doc.file_type?.toUpperCase() || "未知格式"} · 版本 {doc.version}</small></div>
          <p>{doc.content_mode === "METADATA_ONLY" ? "目前只檢索檔名和可得的中繼資料；原檔內容須人工檢視。" : `已擷取 ${doc.characters} 字供搜尋。`}</p>
          {demoSelected && <div className="actions">{doc.category === "IMAGE" && ["png", "jpg", "jpeg", "webp", "gif"].includes(doc.file_type) && <img className="company-image-preview" src={`${DEMO_URL}/preview/${doc.id}`} alt={`預覽 ${doc.title}`} />}
            <a className="button" href={`${DEMO_URL}/files/${doc.id}`} target="_blank" rel="noopener noreferrer">取得原檔</a></div>}
        </article>)}</div>
        {visibleFiles.length > fileLimit && <button onClick={() => setFileLimit((value) => value + 30)}>再顯示 30 份</button>}
        {preview.map((hit) => <article className="card" key={hit.id}><strong>{hit.title}</strong><p>{hit.snippet}</p><small>服務器相關分數 {Math.round(hit.score * 100)}% · 待本案判斷</small></article>)}
      </>}
    </div>

    {workspace && <div className="card"><h4>工作空間預設檢索來源</h4>{policyFields(workspace, setWorkspace)}
      <button disabled={!!pending} onClick={() => run("保存知識來源", async () => {
        setWorkspace(await api("/settings/knowledge", "PATCH", workspace)); setMessage("工作空間來源設定已保存。");
      })}>保存工作空間設定</button></div>}
    {projectPolicy && <div className="card"><h4>目前專案：{project.name}</h4>
      <label><input type="checkbox" checked={projectPolicy.use_workspace_default}
        onChange={(event) => setProjectPolicy({ ...projectPolicy, use_workspace_default: event.target.checked,
          override: event.target.checked ? null : { ...projectPolicy.effective } })} />沿用工作空間預設</label>
      {!projectPolicy.use_workspace_default && policyFields(projectPolicy.override || projectPolicy.effective,
        (next) => setProjectPolicy({ ...projectPolicy, override: next }))}
      <button disabled={!!pending} onClick={() => run("保存專案來源", async () => {
        setProjectPolicy(await api(`/projects/${project.id}/knowledge-policy`, "PATCH",
          projectPolicy.use_workspace_default ? null : projectPolicy.override)); setMessage("專案來源設定已保存。");
      })}>保存專案設定</button>
    </div>}
    {pending && <p role="status" className="execution-card">{pending}，請稍候…</p>}
    {message && <p role="status" className={`feedback ${/失敗|未提供/.test(message) ? "warning" : "success"}`}>{message}</p>}
  </section>;
}
