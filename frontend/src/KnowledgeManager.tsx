import { useEffect, useState } from "react";
import { api } from "./api";
import { KnowledgeSettings } from "./KnowledgeSettings";
import { V7Settings } from "./V7Settings";
import { jobLabel } from "./KnowledgePanel";

type Tab = "documents" | "graph" | "retrieval" | "policy";
type Props = {
  config: any;
  setConfig: (value: any) => void;
  project: any;
  act: (f: () => Promise<unknown>, message?: string) => Promise<void>;
  onOpenFolder: () => void;
};

export function KnowledgeManager({ config, setConfig, project, act, onOpenFolder }: Props) {
  const [tab, setTab] = useState<Tab>("documents");
  const [documents, setDocuments] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [filter, setFilter] = useState("");
  const [visibleCount, setVisibleCount] = useState(30);
  const [folder, setFolder] = useState<any>(null);
  const [graph, setGraph] = useState<any>(null);
  const [jobs, setJobs] = useState<any[]>([]);
  const [tag, setTag] = useState("");
  const [tagName, setTagName] = useState("");
  const [merge, setMerge] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [topK, setTopK] = useState(5);
  const [useModelRerank, setUseModelRerank] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  async function refresh() {
    const [docs, folderInfo, graphInfo, jobRows] = await Promise.all([
      api<any[]>("/knowledge/documents"), api("/workspace/folder"),
      api("/knowledge/graph/status"), api<any[]>("/knowledge/jobs"),
    ]);
    setDocuments(docs); setFolder(folderInfo); setGraph(graphInfo); setJobs(jobRows);
  }
  useEffect(() => { refresh().catch((e) => setError(e.message)); }, []);
  useEffect(() => {
    if (tab !== "graph") return;
    const timer = window.setInterval(() => refresh().catch((e) => setError(e.message)), 3000);
    return () => window.clearInterval(timer);
  }, [tab]);
  async function run(label: string, action: () => Promise<void>) {
    setBusy(label); setError(""); setFeedback("");
    try { await action(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }
  const filtered = documents.filter((d) => `${d.title} ${d.kind} ${d.status}`.toLowerCase().includes(filter.toLowerCase()));
  const tags = (graph?.graph_nodes || []).filter((n: any) => n.kind === "TAG" && (!n.meta?.status || n.meta.status === "ACTIVE"));
  const activeJobs = jobs.filter((j) => !j.project_id && ["QUEUED", "RUNNING", "WAITING_RETRY", "PAUSED", "PARTIAL", "INTERRUPTED"].includes(j.status));
  const graphNodes = (graph?.graph_nodes || []).slice(0, 24);
  const positions = new Map<string, { x: number; y: number }>(graphNodes.map((n: any, i: number) => [n.id, {
    x: 300 + 230 * Math.cos((i * 2 * Math.PI) / graphNodes.length),
    y: 190 + 135 * Math.sin((i * 2 * Math.PI) / graphNodes.length),
  }]));

  return <section className="knowledge-manager">
    <p>管理這台主機的共用知識。遠端 Agent 可以檢索有效的共用文件，但其他使用者的專案文件不會加入此清單。</p>
    <div className="settings-tabs" role="tablist" aria-label="知識庫管理">
      {([ ["documents", "文件"], ["graph", "圖譜"], ["retrieval", "檢索"], ["policy", "來源權限"] ] as const).map(([key, title]) =>
        <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{title}</button>)}
    </div>
    {tab === "documents" && <>
      <div className="card"><h3>共用文件 · {documents.filter((d) => d.searchable).length} 份可檢索</h3>
        <p>專用資料夾：{folder?.path || "尚未設定"} · 已索引 {folder?.files || 0} 份</p>
        <div className="actions"><button onClick={onOpenFolder}>設定資料夾路徑</button>
          <button disabled={!!busy || !folder?.configured} onClick={() => run("正在讀取資料夾", async () => {
            const result = await api("/workspace/folder/scan", "POST"); await refresh();
            setFeedback(result.message);
          })}>重新讀取資料夾</button></div>
        <label>新增共用文件<input type="file" accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md" disabled={!!busy} onChange={(e) => {
          const file = e.target.files?.[0]; e.target.value = ""; if (!file) return;
          void run("正在解析文件", async () => { const form = new FormData(); form.append("file", file); form.append("kind", "BUSINESS_KNOWLEDGE");
            await api("/knowledge/upload", "POST", form); await refresh(); setFeedback(`已加入共用知識庫：${file.name}`); });
        }} /></label>
        <p className="muted">共用文件會提供給已配對的遠端 Agent；請只加入允許共享的資料。</p>
      </div>
      <label>搜尋文件<input value={filter} onChange={(e) => { setFilter(e.target.value); setVisibleCount(30); }} placeholder="名稱、分類或狀態" /></label>
      <div className="kb-document-list">{filtered.length === 0 && <p>沒有符合的共用文件。</p>}
        {filtered.slice(0, visibleCount).map((d) => <article className="card" key={d.id}>
          <div className="kb-row"><strong>{d.title}</strong><span className={`badge ${d.searchable ? "" : "warning"}`}>{d.searchable ? "可檢索" : d.status === "ARCHIVED" ? "已停用" : d.kind === "MEETING_TRANSCRIPT" ? "逐字稿不納入檢索" : d.status}</span></div>
          <p>{d.kind} · {d.characters} 字 · {d.chunks} 段 · {d.graph_edges} 筆圖譜關係 · {d.linked_projects} 個專案採用</p>
          <div className="actions"><button disabled={!!busy} onClick={() => run("讀取文件", async () => setSelected(await api(`/knowledge/documents/${d.id}`)))}>檢視內容</button>
            {d.status === "ACTIVE" && <button disabled={!!busy} onClick={() => run("停用文件", async () => {
              await api(`/knowledge/documents/${d.id}`, "PATCH", { active: false }); await refresh();
              setFeedback("文件已停用；已採用的專案會標示需要重新分析。");
            })}>停用</button>}
            {d.status === "ARCHIVED" && <button disabled={!!busy} onClick={() => run("恢復文件", async () => {
              await api(`/knowledge/documents/${d.id}`, "PATCH", { active: true }); await refresh(); setFeedback("文件已恢復檢索。");
            })}>恢復</button>}</div>
        </article>)}
        {filtered.length > visibleCount && <button onClick={() => setVisibleCount(visibleCount + 30)}>顯示更多（尚有 {filtered.length - visibleCount} 份）</button>}
      </div>
      {selected && <div className="card"><div className="kb-row"><h3>{selected.title}</h3><button onClick={() => setSelected(null)}>關閉內容</button></div>
        <p>{selected.kind} · {selected.status} · 內容預覽最多 12,000 字</p><pre className="kb-preview">{selected.content_preview}</pre></div>}
    </>}
    {tab === "graph" && <>
      <div className="card"><h3>共用知識圖譜</h3><p>{graph?.documents_covered || 0}/{graph?.total_documents || 0} 份文件已覆蓋 · {graph?.nodes || 0} 個節點 · {graph?.edges || 0} 筆關係</p>
        <p>生成標籤與圖譜可能耗時較久。工作狀態保存在主機，離開此頁後仍會繼續。</p>
        <div className="actions"><button disabled={!!busy} onClick={() => run("建立標籤工作", async () => { await api("/knowledge/tag-jobs", "POST", { project_id: null }); await refresh(); setFeedback("標籤工作已排入，請查看下方進度。"); })}>生成標籤</button>
          <button disabled={!!busy} onClick={() => run("建立圖譜工作", async () => { await api("/knowledge/graph-jobs", "POST", { project_id: null }); await refresh(); setFeedback("圖譜工作已排入，請查看下方進度。"); })}>建立／更新圖譜</button></div>
      </div>
      {activeJobs.map((j) => <article className="execution-card" key={j.id} data-state={j.status}><strong>{j.kind === "GRAPH_BUILD" ? "知識圖譜" : "自動標籤"} · {jobLabel(j.status)}</strong>
        <progress max={100} value={j.progress_percent} aria-label="建置進度" /><p>{j.progress_percent}% · {jobLabel(j.stage)} · 完成 {j.completed_items}/{j.total_items} · 失敗 {j.failed_items}</p>
        <small>心跳 {new Date(j.heartbeat_at).toLocaleTimeString()}</small><div className="actions">
          {([ ["pause", "暫停", ["QUEUED", "RUNNING", "WAITING_RETRY"]], ["resume", "繼續", ["PAUSED", "INTERRUPTED"]], ["retry", "重試失敗項目", ["PARTIAL"]], ["cancel", "取消", ["QUEUED", "RUNNING", "WAITING_RETRY", "PAUSED"]] ] as const).map(([action, title, statuses]) => (statuses as readonly string[]).includes(j.status) &&
            <button key={action} disabled={!!busy} onClick={() => run(title, async () => { await api(`/knowledge/jobs/${j.id}/${action}`, "POST"); await refresh(); })}>{title}</button>)}
        </div>{j.errors?.length > 0 && <details><summary>失敗詳情（{j.errors.length}）</summary>{j.errors.map((item: any) => <p key={item.item_id}>{item.error}</p>)}</details>}</article>)}
      <div className="card"><h3>標籤管理</h3><p>目前有效標籤 {tags.length} 個。可修正命名、拒用或合併重複標籤。</p>
        <select aria-label="選擇標籤" value={tag} onChange={(e) => { const id = e.target.value; setTag(id); setTagName(tags.find((n: any) => n.id === id)?.label || ""); }}><option value="">選擇標籤</option>{tags.map((n: any) => <option value={n.id} key={n.id}>{n.label}</option>)}</select>
        {tag && <><label>標籤名稱<input value={tagName} onChange={(e) => setTagName(e.target.value)} /></label>
          <div className="actions"><button disabled={!tagName.trim() || !!busy} onClick={() => run("重新命名標籤", async () => { await api(`/knowledge/tags/${tag}`, "PATCH", { label: tagName }); await refresh(); setFeedback("標籤已更新。"); })}>重新命名</button>
            <button disabled={!!busy} onClick={() => run("拒用標籤", async () => { await api(`/knowledge/tags/${tag}`, "PATCH", { reject: true }); setTag(""); await refresh(); setFeedback("標籤已拒用。"); })}>拒用</button></div>
          <label>合併至<select value={merge} onChange={(e) => setMerge(e.target.value)}><option value="">選擇目標標籤</option>{tags.filter((n: any) => n.id !== tag).map((n: any) => <option value={n.id} key={n.id}>{n.label}</option>)}</select></label>
          <button disabled={!merge || !!busy} onClick={() => run("合併標籤", async () => { await api(`/knowledge/tags/${tag}`, "PATCH", { merge_into: merge }); setTag(""); setMerge(""); await refresh(); setFeedback("標籤已合併。"); })}>合併標籤</button>
        </>}
      </div>
      <details className="card"><summary>查看圖譜節點與關係</summary>
        {graphNodes.length > 0 && <svg viewBox="0 0 600 380" role="img" aria-label="共用知識圖譜預覽" className="graph-canvas">
          {(graph?.graph_edges || []).filter((e: any) => positions.has(e.origin) && positions.has(e.target)).slice(0, 80).map((e: any) => {
            const a = positions.get(e.origin)!, b = positions.get(e.target)!;
            return <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#cda5b2"><title>{e.relation}</title></line>;
          })}
          {graphNodes.map((n: any) => { const p = positions.get(n.id)!; return <g key={n.id}>
            <circle cx={p.x} cy={p.y} r={9} fill={n.kind === "DOCUMENT" ? "#bc405b" : "#289786"} />
            <text x={p.x} y={p.y - 14} textAnchor="middle" fontSize="11">{n.label.slice(0, 13)}</text>
            <title>{n.kind}：{n.label}</title></g>; })}
        </svg>}
        <div className="kb-graph-list">{(graph?.graph_nodes || []).map((n: any) => <p key={n.id}>{n.kind} · {n.label}</p>)}</div>
        <p>預覽最多 24 個節點，清單最多 150 個；檢索會查詢完整圖譜。</p></details>
    </>}
    {tab === "retrieval" && <>
      <div className="card"><h3>檢索試查</h3><p>只對主機共用知識庫試查。預設快速預覽僅檢查詞彙命中，不代表完整檢索分數或關鍵證據；變更設定後請先保存，再選完整試查驗證實際檢索。</p>
        <div className="actions"><input aria-label="試查關鍵字" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="輸入業務規則、產品或系統名稱" />
          <label>Top-K<input type="number" min={1} max={20} value={topK} onChange={(e) => setTopK(Number(e.target.value))} /></label>
          <button className="primary" disabled={!query.trim() || !!busy} onClick={() => run(useModelRerank ? "正在執行完整檢索與評分" : "正在檢查詞彙命中", async () => { setResults([]); const response = await api("/knowledge/search-preview", "POST", { query, top_k: topK, use_model_rerank: useModelRerank }, useModelRerank ? 240000 : 30000); setResults(response.results); setFeedback(`試查完成：${response.total} 份來源（${response.mode === "FAST_LEXICAL" ? "快速詞彙預覽" : "完整檢索"}）。`); })}>試查</button></div>
        <label><input type="checkbox" checked={useModelRerank} onChange={(e) => setUseModelRerank(e.target.checked)} />使用完整檢索流程（包含已啟用的向量、圖譜與模型重排；可能需要數分鐘）</label>
        {busy && <p className="execution-card" role="status">{busy} · 已等待 {elapsed} 秒。{useModelRerank ? "索引、圖譜或模型可能仍在運作，請耐心等候。" : "請稍候…"}</p>}
        {results.map((r: any, i) => <article className="card" key={`${r.source_id}-${i}`}><strong>{r.title}</strong><p>{r.text?.slice(0, 360)}</p>
          <small>{r.evidence_role === "PREVIEW_ONLY" ? `詞彙命中 ${Math.round((r.keyword_overlap || 0) * 100)}% · 僅供預覽` : `本案相關分數 ${Math.round((r.case_relevance_score || 0) * 100)}% · ${r.evidence_role}`} · {(r.retrieval_channels || []).join("＋")} · {r.case_relevance_reason}</small></article>)}
      </div>
      <V7Settings tab="retrieval" config={config} setConfig={setConfig} act={act} />
    </>}
    {tab === "policy" && <KnowledgeSettings project={project} act={act} />}
    {busy && tab !== "retrieval" && <p className="execution-card" role="status">{busy} · 已等待 {elapsed} 秒，請稍候…</p>}
    {feedback && <p className="feedback success" role="status">{feedback}</p>}
    {error && <p className="feedback warning" role="alert">{error}</p>}
  </section>;
}
