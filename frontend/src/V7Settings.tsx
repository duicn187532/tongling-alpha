import { useEffect, useState } from "react";
import { api } from "./api";

const qwenRetrievalPreview = {
  lexical_chunks: 50,
  semantic_chunks: 50,
  candidate_documents: 40,
  aggregation: "max",
  fusion: "minmax",
  semantic: true,
  embedding: {
    model: "qwen3-embedding:4b",
    batch_size: 8,
    timeout: 180,
    query_prefix:
      "Instruct: Retrieve relevant passages from Chinese and English financial system requirements and project documents that answer the query\nQuery: ",
  },
  rerank: false,
  authority: false,
  temporal: false,
  structured: false,
  minimum_score: 0.7,
  allow_local_fallback: false,
  trace: true,
};

export function V7Settings({
  tab,
  config,
  setConfig,
  act,
}: {
  tab: string;
  config: any;
  setConfig: (c: any) => void;
  act: (f: () => Promise<unknown>, m?: string) => Promise<void>;
}) {
  const [catalog, setCatalog] = useState<any>(null),
    [sections, setSections] = useState<any[]>([]),
    [name, setName] = useState("我的金融 PRD"),
    [feedback, setFeedback] = useState(""),
    [models, setModels] = useState<any>(null),
    [routerPreview, setRouterPreview] = useState<any>(null),
    [providers, setProviders] = useState<any[]>([]);
  useEffect(() => {
    api("/prd/templates")
      .then(setCatalog)
      .catch((e) => setFeedback(e.message));
    api<any[]>("/research/providers")
      .then(setProviders)
      .catch(() => {});
  }, []);
  const selected = config.prd_template || "FINANCIAL_STANDARD";
  useEffect(() => {
    if (catalog) {
      const t =
        catalog.templates.find((t: any) => t.id === selected) ||
        catalog.templates[0];
      setSections(t.sections);
    }
  }, [catalog, selected]);
  const update = (k: string, v: any) => setConfig({ ...config, [k]: v });
  const router = config.model_router || {};
  const textModels: string[] = models?.text_models || [];
  const decisionModels: string[] = models?.decision_models || [];
  const routerField = (k: string, v: any) =>
    update("model_router", { ...router, schema_version: 2, [k]: v });
  const routerToggle = (k: string, enabled: boolean) =>
    routerField(k, { ...(router[k] || {}), enabled });
  const org = config.organization || {};
  const orgField = (k: string, v: any) =>
    update("organization", { ...org, [k]: v });
  const move = (from: number, to: number) => {
    if (to < 0 || to >= sections.length) return;
    const next = [...sections];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    setSections(next);
  };
  const save = () =>
    act(async () => {
      await api("/settings", "PUT", config);
      setFeedback("設定已保存。新工作將使用這份設定。");
    });
  return (
    <div className="v7-settings">
      {tab === "output" && (
        <>
          <h3>PRD 模板與章節</h3>
          <p>
            模板只決定預覽與匯出；完整需求、決策及證據仍保留。已定版文件維持原樣。
          </p>
          <label>
            PRD 模板
            <select
              value={selected}
              onChange={(e) => update("prd_template", e.target.value)}
            >
              {catalog?.templates.map((t: any) => (
                <option value={t.id} key={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <p>
            可勾選章節、拖曳或使用上下按鈕排序，並修改標題；變更後請保存為自訂模板。
          </p>
          <div className="template-sections">
            {sections.map((s: any, i: number) => (
              <div
                key={s.key}
                className="template-row"
                draggable
                onDragStart={(e) =>
                  e.dataTransfer.setData("text/plain", String(i))
                }
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  move(Number(e.dataTransfer.getData("text/plain")), i);
                }}
              >
                <input
                  aria-label={"顯示 " + s.title}
                  type="checkbox"
                  checked={s.enabled !== false}
                  onChange={(e) =>
                    setSections(
                      sections.map((x, n) =>
                        n === i ? { ...x, enabled: e.target.checked } : x,
                      ),
                    )
                  }
                />
                <input
                  aria-label={"章節標題 " + (i + 1)}
                  value={s.title}
                  onChange={(e) =>
                    setSections(
                      sections.map((x, n) =>
                        n === i ? { ...x, title: e.target.value } : x,
                      ),
                    )
                  }
                />
                <button
                  aria-label={"上移 " + s.title}
                  onClick={() => move(i, i - 1)}
                  disabled={i === 0}
                >
                  ↑
                </button>
                <button
                  aria-label={"下移 " + s.title}
                  onClick={() => move(i, i + 1)}
                  disabled={i === sections.length - 1}
                >
                  ↓
                </button>
              </div>
            ))}
          </div>
          <label>
            新增章節
            <select
              value=""
              onChange={(e) => {
                const s = catalog.sections.find(
                  (s: any) => s.key === e.target.value,
                );
                if (s)
                  setSections([
                    ...sections,
                    { key: s.key, title: s.title, enabled: true },
                  ]);
              }}
            >
              <option value="">選擇尚未加入的章節</option>
              {catalog?.sections
                .filter((s: any) => !sections.some((x) => x.key === s.key))
                .map((s: any) => (
                  <option key={s.key} value={s.key}>
                    {s.title}
                  </option>
                ))}
            </select>
          </label>
          {["traceability", "controls", "acceptance"].some(
            (k) => !sections.some((s) => s.key === k && s.enabled !== false),
          ) && (
            <p className="feedback warning">
              有治理章節未顯示；內部資料與人工關卡不會被刪除或略過。
            </p>
          )}
          <label>
            自訂模板名稱
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <button
            onClick={() =>
              act(async () => {
                const t = await api("/prd/templates", "POST", {
                  name,
                  sections,
                });
                const next = { ...config, prd_template: t.id };
                setConfig(next);
                await api("/settings", "PUT", next);
                setCatalog(await api("/prd/templates"));
                setFeedback("自訂模板已保存並選用。");
              })
            }
          >
            保存自訂模板並套用
          </button>
        </>
      )}
      {tab === "organization" && (
        <>
          <h3>組織與同業搜尋</h3>
          {[
            ["name", "組織名稱"],
            ["industry", "使用者行業（必填）"],
            ["sub_industry", "次產業"],
            ["region", "地區／市場"],
            ["default_research_language", "研究語言"],
          ].map(([k, title]) => (
            <label key={k}>
              {title}
              <input
                value={org[k] || ""}
                onChange={(e) => orgField(k, e.target.value)}
              />
            </label>
          ))}
          {[
            ["peer_companies", "同業公司"],
            ["peer_keywords", "同業關鍵字"],
            ["regulatory_keywords", "法規關鍵字"],
          ].map(([k, title]) => (
            <label key={k}>
              {title}（每行一項）
              <textarea
                value={(org[k] || []).join("\n")}
                onChange={(e) => orgField(k, e.target.value.split("\n"))}
              />
            </label>
          ))}
          <label>
            搜尋服務
            <select
              value={config.research_provider || "google_news"}
              onChange={(e) => update("research_provider", e.target.value)}
            >
              <option value="google_news">Google News RSS（免金鑰）</option>
              <option value="tavily">Tavily</option>
              <option value="brave">Brave Search</option>
            </select>
          </label>
          <p>
            公開新聞索引可能僅有摘要或限流；全文無法取得時會清楚標示。API
            金鑰使用後端 TAVILY_API_KEY／BRAVE_SEARCH_API_KEY。
          </p>
          {providers.map((p) => (
            <small key={p.provider}>
              {p.provider}：{p.message}
              <br />
            </small>
          ))}
          <label>
            Agent 公開搜尋主題
            <input
              value={config.research_topic || ""}
              onChange={(e) => update("research_topic", e.target.value)}
              placeholder="例如：行動銀行 信用卡預借現金"
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={!!config.research_enabled}
              onChange={(e) => update("research_enabled", e.target.checked)}
            />
            允許 Agent 自動外部搜尋（只傳行業、公開主題與同業，不傳內部全文）
          </label>
          <label>
            最多保留報導數
            <input
              type="number"
              min={1}
              max={10}
              value={config.research_limit ?? 5}
              onChange={(e) => update("research_limit", Number(e.target.value))}
            />
          </label>
        </>
      )}
      {tab === "retrieval" && (
        <>
          <h3>新版檢索預覽</h3>
          <p role="status">
            目前使用：{config.retrieval_version === "v2" ? "新版檢索預覽" : "既有檢索"}。
            新版使用本機 Qwen3 Embedding 4B；開發集已測試，仍需獨立驗收。
          </p>
          <div className="actions">
            <button
              type="button"
              onClick={() =>
                setConfig({
                  ...config,
                  retrieval_version: "v2",
                  retrieval_v2: qwenRetrievalPreview,
                })
              }
            >
              套用 Qwen3 預覽設定
            </button>
            <button
              type="button"
              onClick={() => update("retrieval_version", "v1")}
            >
              使用既有檢索
            </button>
          </div>
          <p>選擇後按下方「保存此頁設定」。若本機模型無法執行，會明確提示錯誤。</p>
          <h3>混合與圖譜檢索</h3>
          <p>
            BM25＋向量持續使用；圖譜為額外的候選來源。圖譜實驗功能預設關閉，建議以實際資料評測後啟用。
          </p>
          <label>
            <input
              type="checkbox"
              checked={!!config.graph_retrieval_enabled}
              onChange={(e) =>
                update("graph_retrieval_enabled", e.target.checked)
              }
            />
            加入圖譜候選
          </label>
          <label>
            <input
              type="checkbox"
              checked={!!config.reranker_enabled}
              onChange={(e) => update("reranker_enabled", e.target.checked)}
            />
            使用模型重新評估候選與本案相關性（較耗時）
          </label>
          {[
            ["graph_document_top_k", "圖譜文件 Top-K", 3, 1, 20, 1],
            ["graph_hop_limit", "圖譜跳數", 2, 1, 3, 1],
            ["unknown_threshold", "未知／排除門檻", 0.2, 0, 1, 0.05],
            ["case_relevance_threshold", "相關來源門檻", 0.45, 0, 1, 0.05],
            ["key_evidence_threshold", "關鍵證據門檻", 0.75, 0, 1, 0.05],
          ].map(([k, title, value, min, max, step]) => (
            <label key={String(k)}>
              {title}
              <input
                type="number"
                min={Number(min)}
                max={Number(max)}
                step={Number(step)}
                value={config[k] ?? value}
                onChange={(e) => update(String(k), Number(e.target.value))}
              />
            </label>
          ))}
          <p>
            相關分數不是正確率；KEY
            還需符合本案範圍、內部權威與有效版本。同業報導始終為
            EXTERNAL_BENCHMARK。
          </p>
        </>
      )}
      {tab === "routing" && (
        <>
          <h3>模型路由與執行政策</h3>
          <p>只影響新工作；進行中的 Agent 會沿用開始時保存的設定。切回「舊版」即可回退模型路由。</p>
          <button disabled={!models} onClick={() => {
            const available = new Set<string>(textModels);
            // Preference order follows the EXP-01 benchmarks; only installed models are used.
            const pick = (names: string[]) => names.find((name) => available.has(name));
            const candidates: Record<string, string | undefined> = {
              micro_worker: pick(["qwen3.5:2b"]), rag_worker: pick(["ministral-3:3b", "qwen3.5:4b"]),
              primary_reasoner: pick(["gemma4:12b", "gemma4:latest"]), agent_worker: pick(["qwen3.5:2b"]),
            };
            const role_to_model = Object.fromEntries(Object.entries(candidates)
              .filter(([, model]) => model).map(([role, model]) => [role, { model }]));
            const fallbacks: Record<string, string | undefined> = {
              micro_worker: pick(["qwen3.5:4b"]), rag_worker: pick(["qwen3.5:4b"]),
              primary_reasoner: pick(["gemma4:latest", "gemma4:12b"].filter((name) => name !== candidates.primary_reasoner)),
            };
            const fallback_chain = { ...(router.fallback_chain || {}),
              ...Object.fromEntries(Object.entries(fallbacks).filter(([, model]) => model)) };
            setConfig({ ...config, graph_retrieval_enabled: true, model_router: {
              ...router, schema_version: 2, mode: "active", role_to_model, fallback_chain,
              task_to_role: {
                TAG_EXTRACTION: "micro_worker", ENTITY_EXTRACTION: "micro_worker",
                RELATION_EXTRACTION: "micro_worker", GRAPH_QUERY_SEED: "micro_worker",
                SOURCE_FACT_EXTRACTION: "rag_worker", MEETING_FACT_EXTRACTION: "primary_reasoner",
                AGENT_EVIDENCE_JUDGEMENT: "primary_reasoner",
                GRAPH_REASONING: "primary_reasoner", ARCHITECTURE_IMPACT: "primary_reasoner",
                PRD_ASSIST: "primary_reasoner", PEER_ANALYSIS: "primary_reasoner",
              },
              ...Object.fromEntries([
                "inference_policy", "evidence_package", "agent_split", "graph_reasoning",
                "architecture_routing", "prd_assist", "escalation", "resource_manager",
                "evaluation", "trace",
              ].map((key) => [key, { enabled: true }])),
              decision: { mode: decisionModels.length ? "advisory" : "off",
                model: ["nimble-v3:latest", "d1:latest"].find((name) => decisionModels.includes(name))
                  || decisionModels[0] || "nimble-v3:latest" },
            } });
            setFeedback("已套用候選開關；模型只從剛探索到的本機清單選取。請保存後檢查路由。");
          }}>套用候選配置與開關</button>
          <p>先按「探索可用模型」，再套用候選配置；未安裝模型不會自動填入。</p>
          <label>
            路由模式
            <select
              value={router.mode || "legacy"}
              onChange={(e) => routerField("mode", e.target.value)}
            >
              <option value="legacy">舊版：沿用目前模型</option>
              <option value="shadow">觀察：保留目前模型與執行結果</option>
              <option value="active">啟用：依任務及角色選擇模型</option>
            </select>
          </label>
          <div className="template-sections">
            {[
              ["inference_policy", "推理與輸出限制"],
              ["evidence_package", "證據引用整理"],
              ["agent_split", "Agent 證據判斷"],
              ["graph_reasoning", "圖譜關係解釋"],
              ["architecture_routing", "系統架構影響分析"],
              ["prd_assist", "PRD 候選建議"],
              ["escalation", "失敗後升級處理"],
              ["resource_manager", "重型模型資源管理"],
              ["evaluation", "效益紀錄"],
              ["trace", "執行紀錄"],
            ].map(([key, label]) => (
              <label key={key}>
                <input type="checkbox" checked={!!router[key]?.enabled}
                  onChange={(e) => routerToggle(key, e.target.checked)} />
                {label}
              </label>
            ))}
          </div>
          <label>
            Decision 判斷模式
            <select value={router.decision?.mode || "off"}
              onChange={(e) => routerField("decision", { ...(router.decision || {}), mode: e.target.value })}>
              <option value="off">關閉</option>
              <option value="advisory">建議：不代替人工確認</option>
            </select>
          </label>
          <p>Decision 自動放行須先完成金融案例校準，目前只提供建議。</p>
          <label>Decision 模型
            <select value={router.decision?.model || "nimble-v3:latest"}
              onChange={(e) => routerField("decision", { ...(router.decision || {}), model: e.target.value })}>
              {!decisionModels.includes(router.decision?.model || "nimble-v3:latest") &&
                <option value={router.decision?.model || "nimble-v3:latest"}>{router.decision?.model || "nimble-v3:latest"}（尚未查驗）</option>}
              {decisionModels.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <button
            onClick={() =>
              act(async () => setModels(await api("/settings/models")))
            }
          >
            探索可用模型
          </button>
          <button onClick={() => act(async () => setRouterPreview(await api("/admin/model-router/preview")))}>
            檢查路由與最近執行
          </button>
          {models && <details open className="technical-group">
            <summary>本機模型目錄 · {models.model_details?.length || 0} 個已安裝／查驗</summary>
            <p>文字模型可用於任務角色；向量模型用於檢索；Decision 模型只用於人工覆核建議。無法查驗的模型不會列入可選清單。</p>
            {(["文字生成", "向量嵌入", "Decision 判斷", "其他／無法判定", "無法查驗"] as const).map((category) => {
              const rows = (models.model_details || []).filter((item: any) => item.category === category);
              return rows.length > 0 && <div key={category} className="template-sections">
                <strong>{category} · {rows.length}</strong>
                {rows.map((item: any) => <div key={item.name} className="template-row">
                  <strong>{item.name}</strong>
                  <small>{item.status === "READY" ? `能力：${item.capabilities.join("、")}${item.thinking?.length ? `；思考：${item.thinking.join("／")}` : ""}` : item.reason}</small>
                </div>)}
              </div>;
            })}
          </details>}
          <datalist id="discovered-text-models">
            {textModels.map((m: string) => (
              <option key={m} value={m} />
            ))}
          </datalist>
          {["micro_worker", "rag_worker", "primary_reasoner", "agent_worker"].map((role) => (
            <label key={role}>
              {role} 模型
              <input list="discovered-text-models" value={router.role_to_model?.[role]?.model || ""}
                placeholder="空白沿用主模型"
                onChange={(e) => routerField("role_to_model", {
                  ...(router.role_to_model || {}),
                  [role]: e.target.value ? { model: e.target.value } : {},
                })} />
            </label>
          ))}
          {["worker", "reasoning"].map((role) => (
            <label key={role}>
              {role === "worker" ? "Worker 模型" : "Reasoning 模型"}
              <input
                list="discovered-text-models"
                value={config.model_roles?.[role]?.model || ""}
                placeholder="空白沿用主模型"
                onChange={(e) =>
                  update("model_roles", {
                    ...config.model_roles,
                    [role]: e.target.value
                      ? {
                          provider: config.provider,
                          endpoint: config.endpoint,
                          model: e.target.value,
                        }
                      : {},
                  })
                }
              />
            </label>
          ))}
          <label>
            最大並行模型工作
            <input
              type="number"
              min={1}
              max={4}
              value={config.model_concurrency ?? 2}
              onChange={(e) =>
                update("model_concurrency", Number(e.target.value))
              }
            />
          </label>
          <p>
            採有界排程；遇模型記憶體不足時降低並行。小型本機模型建議先設為 1。
          </p>
          {Object.entries(models?.tasks || {}).map(([task, role]) => (
            <label key={task}>
              {task}
              <select
                value={(router.mode === "active" ? router.task_to_role?.[task] : config.task_routes?.[task]) || (role as string)}
                onChange={(e) =>
                  router.mode === "active"
                    ? routerField("task_to_role", { ...(router.task_to_role || {}), [task]: e.target.value })
                    : update("task_routes", { ...config.task_routes, [task]: e.target.value })
                }
              >
                <option value="worker">Worker</option>
                <option value="reasoning">Reasoning</option>
                {router.mode === "active" && ["micro_worker", "rag_worker", "primary_reasoner", "agent_worker"].map((name) =>
                  <option value={name} key={name}>{name}</option>)}
              </select>
            </label>
          ))}
          <details>
            <summary>進階推理政策與備援模型</summary>
            {["SOURCE_FACT_EXTRACTION", "MEETING_FACT_EXTRACTION", "GRAPH_REASONING", "ARCHITECTURE_IMPACT", "PRD_ASSIST"].map((task) => {
              const policy = router.policy_by_task?.[task] || {};
              const setPolicy = (patch: any) => routerField("policy_by_task", {
                ...(router.policy_by_task || {}), [task]: { ...policy, ...patch },
              });
              return <div className="template-row" key={task}>
                <strong>{task}</strong>
                <label>思考模式
                  <select value={String(policy.think ?? false)}
                    onChange={(e) => setPolicy({ think: e.target.value === "true" ? true :
                      e.target.value === "false" ? false : e.target.value })}>
                    <option value="false">關閉</option><option value="true">開啟</option>
                    <option value="low">low</option><option value="medium">medium</option><option value="xhigh">xhigh</option>
                  </select>
                </label>
                <label>最大生成量
                  <input type="number" min={64} max={8192} value={policy.num_predict ?? 1800}
                    onChange={(e) => setPolicy({ num_predict: Number(e.target.value) })} />
                </label>
              </div>;
            })}
            {["worker", "reasoning", "micro_worker", "rag_worker", "primary_reasoner"].map((role) =>
              <label key={role}>{role} 備援模型
                <input list="discovered-text-models" value={router.fallback_chain?.[role] || ""}
                  onChange={(e) => routerField("fallback_chain", { ...(router.fallback_chain || {}), [role]: e.target.value })} />
              </label>)}
            <p>不支援的 thinking 值會被拒絕並顯示能力錯誤；每項任務最多補救一次。</p>
          </details>
          {routerPreview && (
            <div className="template-sections">
              <h4>目前路由：{routerPreview.mode} · 設定 {routerPreview.snapshot_id}</h4>
              <p>最近 {routerPreview.sample_count} 次記錄；數量與成功率只是執行狀態，並非金融內容品質分數。</p>
              {routerPreview.routes?.map((item: any) => (
                <div key={item.task} role="status">
                  <strong>{item.task}</strong> → {item.role} → {item.model || "未設定"}
                  <span style={{ color: item.status === "READY" ? "#267348" : "#a23a34" }}>
                    {" "} · {item.status === "READY" ? "可用" : item.reason}
                  </span>
                </div>
              ))}
              {routerPreview.comparison?.map((item: any, i: number) => (
                <p key={i}>{item.task} · {item.mode} · {item.model}：{item.completed}/{item.n} 完成，
                  p50 {item.p50_ms ?? "—"} ms／p95 {item.p95_ms ?? "—"} ms</p>
              ))}
            </div>
          )}
        </>
      )}
      <div className="actions">
        <button className="primary" onClick={save}>
          保存此頁設定
        </button>
      </div>
      {feedback && (
        <p role="status" className="feedback success">
          {feedback}
        </p>
      )}
    </div>
  );
}
