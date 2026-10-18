import { useEffect, useState } from "react";
import { api } from "./api";

const GROUPS: [string, string][] = [
  ["system_components", "系統與責任"], ["interface_contracts", "電文／API 異動"],
  ["data_changes", "資料異動"], ["flow_definitions", "處理與例外流程"],
  ["business_rules", "商業規則"], ["acceptance_cases", "驗收案例"],
];
const AREAS = ["SYSTEM", "INTERFACE", "DATA", "FLOW", "SECURITY", "OPERATIONS"];
const blank = () => Object.fromEntries([...GROUPS.map(([key]) => [key, []]), ["technical_impact", []]]);

export function TechnicalDeliveryPanel({ revision, sources, requirements, isClient, act, onRevisionCreated }: {
  revision: any; sources: any[]; requirements: any[]; isClient: boolean;
  act: (fn: () => Promise<unknown>, message?: string) => Promise<void>;
  onRevisionCreated: (id: string) => void;
}) {
  const [model, setModel] = useState<any>(blank());
  const [hash, setHash] = useState("");
  const [reviews, setReviews] = useState<any[]>([]);
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [role, setRole] = useState("PM");
  const [feedback, setFeedback] = useState("");
  const [architectureAdvice, setArchitectureAdvice] = useState<any>(null);
  const [searchQuery, setSearchQuery] = useState("系統架構 電文 API 欄位 資料異動");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searchWarnings, setSearchWarnings] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchSeconds, setSearchSeconds] = useState(0);
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);
  const [sourceTitle, setSourceTitle] = useState("SA 技術資料補充");
  const [sourceText, setSourceText] = useState("");
  const [proposalSelection, setProposalSelection] = useState<string[]>([]);
  const [packageReady, setPackageReady] = useState(false);
  const [diffDrafts, setDiffDrafts] = useState<Record<string, string>>({});
  const editable = ["DRAFT", "IN_REVIEW"].includes(revision.status);
  useEffect(() => {
    setSelectedSourceIds(sources.filter((source) => source.kind === "TECH_SPEC").map((source) => source.id));
  }, [revision.id]);
  useEffect(() => {
    if (!searching) return;
    setSearchSeconds(0);
    const timer = setInterval(() => setSearchSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [searching]);
  async function reload() {
    const state = await api<any>(`/prd-revisions/${revision.id}/technical-model`);
    setModel(state.model); setHash(state.content_hash); setReviews(state.reviews); setDiffDrafts({});
    setPackageReady(false);
  }
  useEffect(() => {
    void reload().catch((e) => setFeedback(e.message));
    void api<any>(`/prd-revisions/${revision.id}/assist/architecture`).then(setArchitectureAdvice).catch(() => {});
  }, [revision.id, revision.content_hash]);
  function update(key: string, index: number, patch: any) {
    setModel((old: any) => ({ ...old, [key]: old[key].map((item: any, i: number) => i === index ? { ...item, ...patch } : item) }));
  }
  function add(key: string) {
    const item: any = { id: `${key.slice(0, 3).toUpperCase()}-${Date.now().toString(36)}`, name: "", description: "", source_refs: [], requirement_refs: [], status: "CONFIRMED" };
    if (key === "interface_contracts") Object.assign(item, { from_system: "", to_system: "", before: "", after: "", field_diffs: [] });
    if (key === "flow_definitions") Object.assign(item, { steps: [], exception_steps: [] });
    if (key === "data_changes") Object.assign(item, { before: "", after: "", migration: "", rollback: "" });
    if (key === "business_rules") Object.assign(item, { condition: "", on_true: "", on_false: "" });
    if (key === "acceptance_cases") Object.assign(item, { scenario: "NORMAL", expected: "" });
    setModel((old: any) => ({ ...old, [key]: [...old[key], item] }));
  }
  async function save() {
    let submitted = model;
    try {
      submitted = { ...model, interface_contracts: model.interface_contracts.map((item: any) =>
        diffDrafts[item.id] === undefined ? item : { ...item, field_diffs: JSON.parse(diffDrafts[item.id]) }) };
    } catch { setFeedback("欄位差異 JSON 格式不正確，請先修正再儲存。"); return; }
    await act(async () => {
      await api(`/prd-revisions/${revision.id}/technical-model`, "PUT", { expected_hash: hash, model: submitted });
      setFeedback("技術資料已儲存。內容更新後，請重新生成圖表並請 PM／SA／QA 審閱。");
    });
  }
  async function importJson(file?: File) {
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!data || typeof data !== "object" || !GROUPS.every(([key]) => Array.isArray(data[key])) || !Array.isArray(data.technical_impact)) throw new Error("JSON 缺少技術模型章節");
      setModel(data); setFeedback("已讀取 JSON；請核對來源和內容後按儲存。");
    } catch (e) { setFeedback((e as Error).message); }
  }
  async function searchTechnicalSources() {
    if (!searchQuery.trim()) return;
    setSearching(true); setSearchResults([]); setSearchWarnings([]);
    try {
      const response = await api<any>("/retrieval/search", "POST", {
        project_id: revision.project_id, query: searchQuery.trim(),
      }, 300000);
      setSearchResults(response.results || []);
      setSearchWarnings(response.warnings || []);
      setFeedback(`${response.results?.length || 0} 份檢索結果。請核對相關性，將適合的技術文件加入專案。`);
    } catch (error) { setFeedback((error as Error).message); }
    finally { setSearching(false); }
  }
  async function uploadTechnical(file?: File) {
    if (!file) return;
    await act(async () => {
      const form = new FormData(); form.append("file", file); form.append("kind", "TECH_SPEC");
      const source = await api<any>(`/projects/${revision.project_id}/sources/upload`, "POST", form, 120000);
      setSelectedSourceIds((old) => [...new Set([...old, source.id])]);
      setFeedback("技術文件已加入專案並選作分析來源；請檢查內容後執行 AI 分析。");
    });
  }
  const proposedModel = architectureAdvice?.result?.technical_model;
  const proposalCount = proposedModel ? [...GROUPS.map(([key]) => proposedModel[key]?.length || 0),
    proposedModel.technical_impact?.length || 0].reduce((a, b) => a + b, 0) : 0;
  function toggleProposal(key: string) {
    setProposalSelection((old) => old.includes(key) ? old.filter((value) => value !== key) : [...old, key]);
  }
  function applyProposal() {
    if (!proposedModel || !proposalSelection.length) return;
    setModel((old: any) => {
      const next = { ...old };
      const used = new Set(GROUPS.flatMap(([key]) => (old[key] || []).map((item: any) => item.id)));
      for (const [key] of GROUPS) {
        const additions = (proposedModel[key] || []).filter((item: any) =>
          proposalSelection.includes(`${key}:${item.id}`) && !used.has(item.id))
          .map((item: any) => ({ ...item, status: "CONFIRMED" }));
        additions.forEach((item: any) => used.add(item.id));
        next[key] = [...(old[key] || []), ...additions];
      }
      next.technical_impact = [...(old.technical_impact || []).filter((item: any) =>
        !proposalSelection.includes(`impact:${item.area}`)),
        ...(proposedModel.technical_impact || []).filter((item: any) =>
          proposalSelection.includes(`impact:${item.area}`))];
      return next;
    });
    setProposalSelection([]);
    setFeedback("勾選項目已載入編輯區，尚未儲存。請逐項核對來源與細節，補足缺欄後再儲存並請 SA 審閱。");
  }
  return <section className="card technical-delivery" aria-label="技術設計與交付">
    <div className="row"><div><span className="eyebrow">TECHNICAL DESIGN</span><h2>技術設計與 IT 交付</h2></div><span className="badge">{editable ? "可編輯草稿" : "已定版快照"}</span></div>
    <p>先從知識庫檢索本案技術來源；找不到時可上傳 SA 文件或輸入技術說明。AI 只會產生待審候選，電文、欄位與系統責任仍須由 SA 核對。</p>
    {editable && <details open className="technical-group"><summary>1. 檢索與補充技術來源</summary>
      <div className="row"><input aria-label="檢索技術來源" value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)}
        placeholder="系統名稱、電文、API、欄位或專案關鍵字" />
        <button disabled={searching || !searchQuery.trim()} onClick={() => void searchTechnicalSources()}>檢索知識庫</button></div>
      {searching && <div className="execution-card" role="status"><strong>正在檢索 · 已等待 {searchSeconds} 秒</strong>
        <p>正在比對知識庫、圖譜與本案相關性；大型知識庫可能需要較久，請耐心等候。</p></div>}
      {searchWarnings.map((warning, i) => <p className="execution-warning" key={i}>{warning}</p>)}
      {searchResults.map((hit: any) => <div className="technical-item" key={hit.source_id}>
        <strong>{hit.title}</strong>
        <p><small>相關分數 {hit.case_relevance_score ?? hit.score ?? "—"} · 證據角色 {hit.evidence_role || "候選"} · {hit.kind}</small></p>
        <p>{String(hit.text || "").slice(0, 350)}</p>
        {hit.source_location !== "REMOTE" ? <button disabled={hit.evidence_role === "REJECTED" || sources.some((source) => source.id === hit.source_id)}
          onClick={() => void act(async () => {
            await api(`/projects/${revision.project_id}/knowledge/attach`, "POST", { source_id: hit.source_id });
            setSelectedSourceIds((old) => [...new Set([...old, hit.source_id])]);
            setFeedback("已加入專案並選作技術分析來源。");
          })}>{sources.some((source) => source.id === hit.source_id) ? "已在專案" : hit.evidence_role === "REJECTED" ? "與本案不相關" : "加入專案來源"}</button>
          : <small>遠端摘要不能直接作為技術設計依據；請提供可核對的原始文件。</small>}
      </div>)}
      {!searching && searchResults.length === 0 && <p>若檢索不到適合的 SA 來源，請直接補充：</p>}
      <label>上傳 SA 技術文件<input type="file" accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md" onChange={(e) => {
        const file = e.target.files?.[0]; if (file) void uploadTechnical(file); e.target.value = "";
      }} /></label>
      <label>技術說明標題<input value={sourceTitle} onChange={(e) => setSourceTitle(e.target.value)} /></label>
      <label>貼上 SA 技術說明<textarea value={sourceText} onChange={(e) => setSourceText(e.target.value)}
        placeholder="請寫明系統責任、目前與目標電文／資料、欄位及例外處理；不知道的部分請留白。" /></label>
      <button disabled={!sourceTitle.trim() || !sourceText.trim()} onClick={() => void act(async () => {
        const source = await api<any>(`/projects/${revision.project_id}/requirement-text`, "POST",
          { title: sourceTitle.trim(), text: sourceText.trim(), kind: "TECH_SPEC" });
        setSelectedSourceIds((old) => [...new Set([...old, source.id])]);
        setSourceText(""); setFeedback("技術說明已加入專案並選作分析來源。");
      })}>加入技術說明</button>
    </details>}
    {editable && <details open className="technical-group"><summary>2. 選擇來源並生成技術資料</summary>
      <p>只會分析勾選的本案來源。業務需求文件不能自行證明系統架構或電文欄位。</p>
      {sources.map((source) => <label key={source.id} className="template-row">
        <input type="checkbox" checked={selectedSourceIds.includes(source.id)}
          onChange={() => setSelectedSourceIds((old) => old.includes(source.id) ? old.filter((id) => id !== source.id) : [...old, source.id])} />
        {source.title} · {source.kind}
      </label>)}
      {sources.length === 0 && <p>尚無本案來源，請先檢索或補充。</p>}
      <button disabled={!selectedSourceIds.length} onClick={() => void act(async () => {
        setFeedback("正在檢索已選來源並產生技術候選，請查看右側工作卡；較長文件可能耗時數分鐘。");
        setArchitectureAdvice(await api<any>(`/prd-revisions/${revision.id}/assist/architecture`, "POST",
          { source_ids: selectedSourceIds }, 360000));
        setProposalSelection([]);
        setFeedback("技術候選已產生，請核對檢索段落並勾選要採用的項目。");
      })}>AI 分析並產生技術資料</button>
    </details>}
    {architectureAdvice?.result && <details open className="technical-group"><summary>3. AI 技術候選 · {proposalCount} 項</summary>
      <p>候選不會自動寫入技術模型；請先核對來源段落，再勾選要載入編輯區的項目。</p>
      {architectureAdvice.result.retrieved_evidence?.length > 0 && <details><summary>本次檢索到的原文段落 · {architectureAdvice.result.retrieved_evidence.length}</summary>
        {architectureAdvice.result.retrieved_evidence.map((hit: any) => <div className="technical-item" key={hit.chunk_id}>
          <strong>{hit.title}</strong><small>段落 {hit.chunk_id}</small><p>{hit.text}</p>
        </div>)}
      </details>}
      {GROUPS.map(([key, label]) => (proposedModel?.[key] || []).length > 0 && <div key={key}>
        <h4>{label}</h4>{proposedModel[key].map((item: any) => <label className="technical-item" key={item.id}>
          <input type="checkbox" checked={proposalSelection.includes(`${key}:${item.id}`)}
            onChange={() => toggleProposal(`${key}:${item.id}`)} />
          <strong>{item.name}</strong><p>{item.description}</p>
          <small>來源：{item.source_refs?.map((id: string) => sources.find((source) => source.id === id)?.title || id).join("、")} · 原文段落：{item.chunk_refs?.join("、") || "未提供"}</small>
          {architectureAdvice.result.validation_issues?.[`${key}:${item.id}`]?.map((issue: string) =>
            <p className="execution-warning" key={issue}>套用後仍需補正：{issue}</p>)}
        </label>)}
      </div>)}
      {(proposedModel?.technical_impact || []).map((item: any) => <label className="technical-item" key={item.area}>
        <input type="checkbox" checked={proposalSelection.includes(`impact:${item.area}`)}
          onChange={() => toggleProposal(`impact:${item.area}`)} />
        <strong>{item.area} · {item.affected ? "有影響" : "無影響"}</strong><p>{item.rationale}</p><small>原文段落：{item.chunk_refs?.join("、") || "未提供"}</small>
      </label>)}
      {architectureAdvice.result.missing_information?.map((item: string, i: number) => <p key={i}>待補：{item}</p>)}
      {editable && <button disabled={!proposalSelection.length} onClick={applyProposal}>將勾選候選載入編輯區</button>}
    </details>}
    {revision.status === "BASELINED" && <button onClick={() => void act(async () => {
      const next = await api<any>(`/prd-revisions/${revision.id}/technical-amendment`, "POST");
      onRevisionCreated(next.id); setFeedback("已建立技術補件草稿；原定版內容保留。請在新版本填寫並審閱。");
    })}>為此版建立技術補件草稿</button>}
    {editable && <label className="import-technical">匯入技術模型 JSON <input type="file" accept=".json,application/json" onChange={(e) => void importJson(e.target.files?.[0])} /></label>}
    {GROUPS.map(([key, label]) => <details key={key} className="technical-group"><summary>{label} · {model[key]?.length || 0} 項</summary>
      {(model[key] || []).map((item: any, index: number) => <div key={item.id + index} className="technical-item">
        <div className="row"><strong>{item.id}</strong>{editable && <button onClick={() => setModel((old: any) => ({ ...old, [key]: old[key].filter((_: any, i: number) => i !== index) }))}>移除</button>}</div>
        <label>名稱<input disabled={!editable} value={item.name} onChange={(e) => update(key, index, { name: e.target.value })} /></label>
        <label>內容／責任<textarea disabled={!editable} value={item.description} onChange={(e) => update(key, index, { description: e.target.value })} /></label>
        <label>來源文件<select disabled={!editable} value={item.source_refs?.[0] || ""} onChange={(e) => update(key, index, { source_refs: e.target.value ? [e.target.value] : [], chunk_refs: [] })}><option value="">請選擇</option>{sources.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}</select></label>
        <label>關聯需求<select disabled={!editable} value={item.requirement_refs?.[0] || ""} onChange={(e) => update(key, index, { requirement_refs: e.target.value ? [e.target.value] : [] })}><option value="">不指定</option>{requirements.map((r) => <option key={r.id} value={r.id}>{r.topic}</option>)}</select></label>
        {key === "interface_contracts" && <><label>來源系統<select disabled={!editable} value={item.from_system} onChange={(e) => update(key, index, { from_system: e.target.value })}><option value="">請選擇</option>{model.system_components.map((s: any) => <option key={s.id}>{s.name}</option>)}</select></label><label>目標系統<select disabled={!editable} value={item.to_system} onChange={(e) => update(key, index, { to_system: e.target.value })}><option value="">請選擇</option>{model.system_components.map((s: any) => <option key={s.id}>{s.name}</option>)}</select></label><label>異動前<textarea disabled={!editable} value={item.before} onChange={(e) => update(key, index, { before: e.target.value })} /></label><label>異動後<textarea disabled={!editable} value={item.after} onChange={(e) => update(key, index, { after: e.target.value })} /></label><label>欄位差異 JSON（field、before、after、reason）<textarea disabled={!editable} value={diffDrafts[item.id] ?? JSON.stringify(item.field_diffs, null, 2)} onChange={(e) => setDiffDrafts((old) => ({ ...old, [item.id]: e.target.value }))} /></label></>}
        {key === "flow_definitions" && <><label>正常流程（每行一步）<textarea disabled={!editable} value={item.steps.join("\n")} onChange={(e) => update(key, index, { steps: e.target.value.split("\n").filter(Boolean) })} /></label><label>例外流程（每行一步）<textarea disabled={!editable} value={item.exception_steps.join("\n")} onChange={(e) => update(key, index, { exception_steps: e.target.value.split("\n").filter(Boolean) })} /></label></>}
        {key === "data_changes" && <>{([ ["before", "異動前"], ["after", "異動後"], ["migration", "遷移方式"], ["rollback", "回復方式"] ] as [string, string][]).map(([field, title]) => <label key={field}>{title}<textarea disabled={!editable} value={item[field] || ""} onChange={(e) => update(key, index, { [field]: e.target.value })} /></label>)}</>}
        {key === "business_rules" && <>{([ ["condition", "判斷條件"], ["on_true", "條件成立"], ["on_false", "條件不成立"] ] as [string, string][]).map(([field, title]) => <label key={field}>{title}<input disabled={!editable} value={item[field] || ""} onChange={(e) => update(key, index, { [field]: e.target.value })} /></label>)}</>}
        {key === "acceptance_cases" && <><label>情境<select disabled={!editable} value={item.scenario} onChange={(e) => update(key, index, { scenario: e.target.value })}>{["NORMAL", "BOUNDARY", "FAILURE"].map((x) => <option key={x}>{x}</option>)}</select></label><label>預期結果<textarea disabled={!editable} value={item.expected} onChange={(e) => update(key, index, { expected: e.target.value })} /></label></>}
      </div>)}
      {editable && <button onClick={() => add(key)}>新增{label}</button>}
    </details>)}
    <details className="technical-group"><summary>技術影響 YES／NO · {model.technical_impact?.length || 0}/6</summary>{AREAS.map((area) => { const row = (model.technical_impact || []).find((x: any) => x.area === area); return <div key={area} className="technical-item"><strong>{area}</strong><label>是否影響<select disabled={!editable} value={row ? String(row.affected) : ""} onChange={(e) => setModel((old: any) => ({ ...old, technical_impact: [...old.technical_impact.filter((x: any) => x.area !== area), { area, affected: e.target.value === "true", rationale: row?.rationale || "", source_refs: row?.source_refs || [] }] }))}><option value="">未判定</option><option value="true">YES</option><option value="false">NO</option></select></label>{row && <><label>理由<input disabled={!editable} value={row.rationale} onChange={(e) => setModel((old: any) => ({ ...old, technical_impact: old.technical_impact.map((x: any) => x.area === area ? { ...x, rationale: e.target.value } : x) }))} /></label><label>依據文件<select disabled={!editable} value={row.source_refs[0] || ""} onChange={(e) => setModel((old: any) => ({ ...old, technical_impact: old.technical_impact.map((x: any) => x.area === area ? { ...x, source_refs: e.target.value ? [e.target.value] : [], chunk_refs: [] } : x) }))}><option value="">請選擇</option>{sources.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}</select></label></>}</div>; })}</details>
    {editable && <div className="actions"><button className="primary" onClick={() => void save()}>儲存技術資料</button><button onClick={() => void act(async () => { await api(`/prd-revisions/${revision.id}/technical-diagrams`, "POST"); setFeedback("三張圖已由技術模型生成並驗證渲染。"); })}>生成並驗證三張圖</button></div>}
    <h3>版本審閱</h3><p>審閱綁定內容版本；任何模型或圖表變動都須重新審閱。姓名為自行填寫，正式身份仍需由組織流程確認。</p>
    {reviews.map((r, i) => <p key={i}>{r.role} · {r.reviewer} · {r.reviewed_at} · {r.note}</p>)}
    <div className="technical-review-form"><label>角色<select value={role} onChange={(e) => setRole(e.target.value)}>{(isClient ? ["PM"] : ["PM", "SA", "QA"]).map((x) => <option key={x}>{x}</option>)}</select></label><label>審閱者<input value={reviewer} onChange={(e) => setReviewer(e.target.value)} /></label><label>審閱意見<input value={note} onChange={(e) => setNote(e.target.value)} /></label><button onClick={() => void act(async () => { await api(`/prd-revisions/${revision.id}/technical-reviews`, "POST", { role, reviewer, note, expected_hash: hash }); await reload(); setFeedback(`${role} 審閱已記錄`); })}>記錄審閱</button></div>
    {revision.status === "BASELINED" && <div className="actions"><button onClick={() => void act(async () => { await api(`/prd-revisions/${revision.id}/delivery-package`, "POST"); setPackageReady(true); setFeedback("IT 交付包已建立，包含技術模型、三張圖、業務 PRD 與清單。"); })}>檢查並建立 IT 交付包</button>{packageReady && <a href={`/api/prd-revisions/${revision.id}/delivery-package`}>下載 IT 交付包</a>}</div>}
    {feedback && <p role="status" className="feedback">{feedback}</p>}
  </section>;
}
