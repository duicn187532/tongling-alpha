import { useEffect, useState } from "react";
import { api } from "./api";
type Act = (f: () => Promise<unknown>, message?: string) => Promise<void>;
export const jobLabel = (value: string) =>
  (
    ({
      QUEUED: "等待執行",
      RUNNING: "執行中",
      WAITING_RETRY: "等待重試",
      PAUSING: "正在保存並暫停",
      PAUSED: "已暫停",
      COMPLETED: "已完成",
      PARTIAL: "部分完成，請重試失敗項目",
      CANCELLED: "已取消",
      FAILED: "執行失敗",
      DEGRADED: "結果不完整",
      INTERRUPTED: "已中斷，可重新執行",
      PARSE_CHECK: "檢查文件",
      TAG_GENERATION: "產生標籤",
      ENTITY_EXTRACTION: "辨識實體",
      RELATION_EXTRACTION: "建立關係",
      GRAPH_UPSERT: "保存圖譜",
      GRAPH_INDEX: "更新索引",
      VALIDATION: "檢查結果",
      FINALIZE: "完成整理",
    }) as Record<string, string>
  )[value] || value;
export function KnowledgePanel({
  pid,
  config,
  act,
  isClient = false,
}: {
  pid: string;
  config: any;
  act: Act;
  isClient?: boolean;
}) {
  const [jobs, setJobs] = useState<any[]>([]),
    [graph, setGraph] = useState<any>(null),
    [reports, setReports] = useState<any[]>([]),
    [error, setError] = useState(""),
    [topic, setTopic] = useState(config?.research_topic || ""),
    [scope, setScope] = useState("global"),
    [buildSources, setBuildSources] = useState<any[]>([]),
    [selectedSources, setSelectedSources] = useState<string[]>([]),
    [forceRebuild, setForceRebuild] = useState(false),
    [tagName, setTagName] = useState(""),
    [tag, setTag] = useState(""),
    [merge, setMerge] = useState("");
  async function refresh() {
    const [j, g, r] = await Promise.all([
      isClient ? Promise.resolve([]) : api<any[]>("/knowledge/jobs"),
      api("/knowledge/graph/status?project_id=" + pid),
      api<any[]>(`/projects/${pid}/peer-research`),
    ]);
    setJobs(j);
    setGraph(g);
    setReports(r);
    setError("");
  }
  useEffect(() => {
    let disposed = false,
      pending = false;
    async function poll() {
      if (pending) return;
      pending = true;
      try {
        if (!disposed) await refresh();
      } catch (e) {
        if (!disposed) setError((e as Error).message);
      } finally {
        pending = false;
      }
    }
    void poll();
    const t = setInterval(poll, 2500);
    return () => {
      disposed = true;
      clearInterval(t);
    };
  }, [pid]);
  useEffect(() => {
    if (isClient) return;
    let live = true;
    api<any[]>(`/knowledge/build-sources${scope === "project" ? `?project_id=${pid}` : ""}`)
      .then((rows) => { if (live) { setBuildSources(rows); setSelectedSources(rows.map((x) => x.id)); } })
      .catch((e) => { if (live) setError((e as Error).message); });
    return () => { live = false; };
  }, [pid, scope, isClient]);
  const start = (kind: string) =>
    act(async () => {
      if (!selectedSources.length) throw new Error("請至少選擇一份文件");
      await api(`/knowledge/${kind}-jobs`, "POST", {
        project_id: scope === "global" ? null : pid,
        source_ids: selectedSources,
        force: forceRebuild,
      });
      await refresh();
    });
  const nodes = (graph?.graph_nodes || [])
    .filter((n: any) => !n.meta?.status || n.meta.status === "ACTIVE")
    .slice(0, 24);
  const pos = new Map(
    nodes.map((n: any, i: number) => [
      n.id,
      {
        x: 300 + 230 * Math.cos((i * 2 * Math.PI) / nodes.length),
        y: 190 + 135 * Math.sin((i * 2 * Math.PI) / nodes.length),
      },
    ]),
  );
  return (
    <section className="knowledge-tools">
      <h3>標籤與知識圖譜</h3>
      <p>
        {isClient ? "此專案可檢索主機共用知識與圖譜；建置和管理由主機負責。" : "首次建立標籤與知識圖譜可能需要較長時間，請耐心等候。重新整理或切換頁面不會中止後端工作，可稍後回來查看進度。"}
      </p>
      {!isClient && <>
      <label>
        建置範圍
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          <option value="global">共用知識庫</option>
          <option value="project">共用知識庫＋目前專案</option>
        </select>
      </label>
      <div className="actions">
        <button onClick={() => start("tag")}>重新建立標籤</button>
        <button onClick={() => start("graph")}>建立／更新知識圖譜</button>
      </div>
      <details className="source-picker"><summary>選擇建置文件 · 已選 {selectedSources.length}/{buildSources.length} 份（未變更內容會略過）</summary>
        <div className="actions"><button onClick={() => setSelectedSources(buildSources.map((s) => s.id))}>全選</button><button onClick={() => setSelectedSources([])}>清空</button></div>
        {buildSources.map((s) => <label key={s.id} className="source-option"><input type="checkbox" checked={selectedSources.includes(s.id)} onChange={(e) => setSelectedSources((old) => e.target.checked ? [...old, s.id] : old.filter((x) => x !== s.id))} />{s.title}</label>)}
      </details>
      <label className="source-option"><input type="checkbox" checked={forceRebuild} onChange={(e) => setForceRebuild(e.target.checked)} />強制重新處理已建置文件（模型或解析策略改變時使用）</label>
      </>}
      {error && <p role="alert">狀態讀取失敗：{error}</p>}
      {jobs
        .filter((j) => !j.project_id || j.project_id === pid)
        .slice(0, 5)
        .map((j) => (
          <article className="execution-card" data-state={j.status} key={j.id}>
            <header>
              <strong>
                {j.kind === "GRAPH_BUILD" ? "知識圖譜" : "自動標籤"} ·{" "}
                {jobLabel(j.status)}
              </strong>
              <span>{j.progress_percent}%</span>
            </header>
            <progress
              aria-label="建置進度"
              max={100}
              value={j.progress_percent}
            />
            <p>
              {jobLabel(j.stage)} · 完成 {j.completed_items}/{j.total_items} ·
              失敗 {j.failed_items} · 等待重試 {j.retrying}
            </p>
            <small>選取 {j.checkpoint.documents || 0} 份文件 · 略過未變更區塊 {j.checkpoint.skipped_unchanged || 0} 個</small>
            <small>
              版本 {j.checkpoint.build_version?.slice(0, 8)} · 心跳{" "}
              {new Date(j.heartbeat_at).toLocaleTimeString()}
            </small>
            <div className="actions">
              {["QUEUED", "RUNNING", "WAITING_RETRY"].includes(j.status) && (
                <button
                  onClick={() =>
                    act(async () => {
                      await api(`/knowledge/jobs/${j.id}/pause`, "POST");
                      await refresh();
                    })
                  }
                >
                  暫停建置
                </button>
              )}
              {j.status === "PAUSED" && (
                <button
                  onClick={() =>
                    act(async () => {
                      await api(`/knowledge/jobs/${j.id}/resume`, "POST");
                      await refresh();
                    })
                  }
                >
                  恢復建置
                </button>
              )}
              {!["COMPLETED", "CANCELLED", "PARTIAL"].includes(j.status) && (
                <button
                  onClick={() =>
                    act(async () => {
                      await api(`/knowledge/jobs/${j.id}/cancel`, "POST");
                      await refresh();
                    })
                  }
                >
                  取消建置
                </button>
              )}
              {j.failed_items > 0 && j.status !== "CANCELLED" && (
                <button
                  onClick={() =>
                    act(async () => {
                      await api(`/knowledge/jobs/${j.id}/retry`, "POST");
                      await refresh();
                    })
                  }
                >
                  重試失敗項目
                </button>
              )}
            </div>
            {!!j.errors.length && (
              <details>
                <summary>失敗詳情</summary>
                {j.errors.map((e: any) => (
                  <p key={e.item_id}>
                    {e.error}（{e.attempts} 次）
                    <button
                      onClick={() =>
                        act(async () => {
                          await api(
                            `/knowledge/items/${e.item_id}/retry`,
                            "POST",
                          );
                          await refresh();
                        })
                      }
                    >
                      單項重試
                    </button>
                  </p>
                ))}
              </details>
            )}
          </article>
        ))}
      {graph && (
        <details>
          <summary>
            圖譜總覽 · {graph.documents_covered}/{graph.total_documents} 份文件
            · {graph.nodes} 節點／{graph.edges} 關係
          </summary>
          <svg
            viewBox="0 0 600 380"
            role="img"
            aria-label="知識圖譜關係圖"
            className="graph-canvas"
          >
            {graph.graph_edges
              .filter((e: any) => pos.has(e.origin) && pos.has(e.target))
              .slice(0, 80)
              .map((e: any) => {
                const a: any = pos.get(e.origin),
                  b: any = pos.get(e.target);
                return (
                  <line
                    key={e.id}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke="#cda5b2"
                  >
                    <title>{e.relation}</title>
                  </line>
                );
              })}
            {nodes.map((n: any) => {
              const p: any = pos.get(n.id);
              return (
                <g key={n.id}>
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={9}
                    fill={n.kind === "DOCUMENT" ? "#bc405b" : "#289786"}
                  />
                  <text x={p.x} y={p.y - 14} textAnchor="middle" fontSize="11">
                    {n.label.slice(0, 13)}
                  </text>
                  <title>
                    {n.kind}：{n.label}
                  </title>
                </g>
              );
            })}
          </svg>
          <small>
            圖中最多顯示 24
            個節點；搜尋使用全部可用節點。文件／段落關聯的細節保留於搜尋路徑。
          </small>
          {!isClient && <><label>
            標籤管理
            <select
              value={tag}
              onChange={(e) => {
                setTag(e.target.value);
                setTagName(
                  graph.graph_nodes.find((n: any) => n.id === e.target.value)
                    ?.label || "",
                );
              }}
            >
              <option value="">選擇標籤</option>
              {graph.graph_nodes
                .filter((n: any) => n.kind === "TAG")
                .map((n: any) => (
                  <option key={n.id} value={n.id}>
                    {n.label}
                  </option>
                ))}
            </select>
          </label>
          {tag && (
            <>
              <label>
                標籤名稱
                <input
                  value={tagName}
                  onChange={(e) => setTagName(e.target.value)}
                />
              </label>
              <button
                onClick={() =>
                  act(async () => {
                    await api("/knowledge/tags/" + tag, "PATCH", {
                      label: tagName,
                    });
                    await refresh();
                  })
                }
              >
                重新命名
              </button>
              <button
                onClick={() =>
                  act(async () => {
                    await api("/knowledge/tags/" + tag, "PATCH", {
                      reject: true,
                    });
                    await refresh();
                  })
                }
              >
                拒用標籤
              </button>
              <label>
                合併到
                <select
                  value={merge}
                  onChange={(e) => setMerge(e.target.value)}
                >
                  <option value="">選擇目標標籤</option>
                  {graph.graph_nodes
                    .filter((n: any) => n.kind === "TAG" && n.id !== tag)
                    .map((n: any) => (
                      <option key={n.id} value={n.id}>
                        {n.label}
                      </option>
                    ))}
                </select>
              </label>
              <button
                disabled={!merge}
                onClick={() =>
                  act(async () => {
                    await api("/knowledge/tags/" + tag, "PATCH", {
                      merge_into: merge,
                    });
                    setTag("");
                    await refresh();
                  })
                }
              >
                合併並保留別名
              </button>
            </>
          )}</>}
        </details>
      )}
      <h3>同業與產業分析</h3>
      <p>
        行業：
        {config?.organization?.industry ||
          "尚未設定，請先到設定 → 組織與行業填寫。"}
        。報導僅供外部比較，不會自動建立內部需求。
      </p>
      <label>
        公開搜尋主題
        <input
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="請輸入可公開的功能或產品名稱"
        />
      </label>
      <button
        disabled={
          !topic.trim() ||
          reports.some((r) => ["RUNNING", "QUEUED"].includes(r.status))
        }
        onClick={() =>
          act(async () => {
            await api(`/projects/${pid}/peer-research`, "POST", { topic });
            await refresh();
          })
        }
      >
        搜尋並分析同業報導
      </button>
      {reports.slice(0, 3).map((r) => (
        <article className="card" key={r.id}>
          <h4>同業報告 · {jobLabel(r.status)}</h4>
          <p>
            {r.data.report?.["本案外部趨勢摘要"] ||
              "工作已建立，請查看執行狀態卡片。"}
          </p>
          {r.data.errors?.map((e: string, i: number) => (
            <p className="execution-warning" key={i}>
              {e}
            </p>
          ))}
          {r.data.analysis?.map((a: any, i: number) => (
            <div className="evidence" key={`analysis-${i}`}>
              <strong>{a.section}</strong>
              <p>{a.observation}</p>
              <blockquote>{a.quote}</blockquote>
              <small>引用來源：{a.source_refs.join("、")}</small>
            </div>
          ))}
          {r.data.report && (
            <details>
              <summary>完整研討摘要</summary>
              {Object.entries(r.data.report)
                .filter(([k]) => k !== "本案外部趨勢摘要")
                .map(([k, v]) => (
                  <div key={k}>
                    <strong>{k}</strong>
                    <p>{Array.isArray(v) ? v.join("；") : String(v)}</p>
                  </div>
                ))}
            </details>
          )}
          {r.data.findings?.map((f: any, i: number) => (
            <div className="evidence" key={i}>
              <span className="badge">
                外部參考 · 相關度 {Math.round(f.case_relevance_score * 100)}%
              </span>
              <p>{f.capability_or_practice}</p>
              <small>
                {f.applicability} · {f.risk_or_constraint}
              </small>
              <p>
                <a href={f.source_url} target="_blank" rel="noreferrer">
                  查看報導
                </a>
              </p>
            </div>
          ))}
          <details>
            <summary>來源與擷取範圍</summary>
            {r.data.sources?.map((s: any) => (
              <p key={s.source_id}>
                <a href={s.source_url} target="_blank" rel="noreferrer">
                  {s.title}
                </a>{" "}
                · {s.publisher} · {s.published_at || "日期未知"} ·{" "}
                {s.coverage === "SEARCH_SNIPPET" ? "僅搜尋摘要" : "文章節錄"}
                <br />
                {s.fetch_error}
              </p>
            ))}
          </details>
        </article>
      ))}
    </section>
  );
}
