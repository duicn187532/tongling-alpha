import React, { useCallback, useEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import { api } from "./api";
import { ActivityCards, noticeVisible } from "./ActivityCards";
import { V7Settings } from "./V7Settings";
import { KnowledgeSettings } from "./KnowledgeSettings";
import { HostAccess } from "./HostAccess";
import { KnowledgeManager } from "./KnowledgeManager";
import { ClientApp } from "./ClientApp";
import { KnowledgePanel, jobLabel } from "./KnowledgePanel";
import { ProjectManager } from "./ProjectManager";
import { TechnicalDeliveryPanel } from "./TechnicalDeliveryPanel";
import { GapPanel, RequirementRowEditor } from "./PrdEditor";
import { ProjectHub } from "./ProjectHub";
import { AppearanceSettings, initAppearanceRuntime } from "./AppearanceSettings";
import { QueueSettings } from "./QueueSettings";
import { submitAndWait, type Job } from "./jobClient";
import "./styles.css";
import "./theme.css";
import "./appearance.css";

let mermaidModule: Promise<(typeof import("mermaid"))["default"]> | null = null;
function loadMermaid() {
  if (!mermaidModule) mermaidModule = import("mermaid").then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "neutral",
      fontFamily: "Microsoft JhengHei, sans-serif",
    });
    return mermaid;
  }).catch((error) => {
    mermaidModule = null;
    throw error;
  });
  return mermaidModule;
}
// Four stages a PM moves through; meetings and IT delivery are side tabs.
const STAGES = [
  { label: "需求與依據", pages: [1, 2, 3] },
  { label: "審閱提案", pages: [5, 6] },
  { label: "PRD", pages: [8] },
  { label: "版本", pages: [9] },
];
const EXTRAS = [
  { label: "會議", page: 4 },
  { label: "IT 交付", page: 7 },
];
function placeName(page: number) {
  return STAGES.find((stage) => stage.pages.includes(page))?.label ||
    EXTRAS.find((extra) => extra.page === page)?.label || "";
}
const statusLabels: Record<string, string> = {
  READY: "準備就緒",
  RUNNING: "彤靈處理中",
  PAUSED: "已暫停",
  WAITING_HUMAN: "需要你的確認",
  WAITING_MEETING: "等待會議",
  BASELINED: "已建立正式版本",
  ERROR: "需要處理錯誤",
  OPEN: "待釐清",
  CONFLICT: "來源衝突",
  CONFIRMED_CANDIDATE: "待確認候選",
  EXCLUDED: "不納入",
  IN_REVIEW: "待審閱",
  SUPERSEDED: "歷史版本",
  DRAFT: "草稿",
  REVIEWED: "已核准修訂",
  ALREADY_SUPPORTED: "既有能力可支援",
  ADD: "建議新增",
  MODIFY: "建議修改",
  REMOVE: "建議移除",
  UNSUPPORTED: "目前不支援",
  UNKNOWN: "待確認",
  TRUE_CONFLICT: "有效規範衝突",
  CONTEXT: "現行規範（背景）",
  KEY: "關鍵依據",
  SUPPORTING: "支持依據",
  REJECTED: "不相關",
  EXTERNAL_BENCHMARK: "外部參考",
  INTERNAL_AUTHORITY: "內部正式規範",
  UNVERIFIED_INTERNAL: "內部資料（未驗證）",
  CURRENT: "現行有效",
  FUTURE: "尚未生效",
  LEXICAL: "關鍵字",
  SEMANTIC: "語意",
  GRAPH: "圖譜",
  SERVER_SEARCH: "公司伺服器",
  SERVER_GRAPH: "公司伺服器圖譜",
  HIGH_RISK_CHANGE: "放寬控制",
  TECHNICAL_UNSUPPORTED: "技術不支援",
  KEY_INFORMATION_MISSING: "缺關鍵資訊",
  APPLIED: "已套用",
  NO_PRD_CHANGE: "無須改版",
  CANCELLED: "已取消",
};
function label(value: string) {
  return statusLabels[value] || value;
}

function Diagram({ source }: { source: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const id = "m" + Math.random().toString(36).slice(2);
    loadMermaid()
      .then((mermaid) => mermaid.render(id, source))
      .then(({ svg }) => {
        if (live && ref.current) ref.current.innerHTML = svg;
      })
      .catch((e) => {
        if (live) setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [source]);
  return (
    <>
      <div className="diagram" ref={ref} />
      {error && <p className="error">圖表無法預覽：{error}</p>}
    </>
  );
}

type Action = (fn: () => Promise<unknown>, message?: string) => Promise<void>;
function ReviewCard({
  row,
  busy,
  act,
  sourceButton,
  blockedReason,
  criticalReason,
  proposal,
}: {
  row: any;
  busy: boolean;
  act: Action;
  sourceButton: (id: string) => React.ReactNode;
  blockedReason: string;
  criticalReason?: string;
  proposal?: any;
}) {
  const suggestion = proposal?.proposed_requirement?.statement || row.statement;
  const [editing, setEditing] = useState(false);
  const [statement, setStatement] = useState(row.decision.statement || suggestion);
  const [note, setNote] = useState("");
  const [category, setCategory] = useState(row.status === "EXCLUDED" ? "SCOPE_EXCLUSION" : row.decision.category || "FUNCTIONAL");
  const [saving, setSaving] = useState("");
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const saved = row.decision || {};
  const decisionLabels: Record<string, string> = { ACCEPT: "已採納", MODIFY: "已修改採納", REJECT: "已拒絕", PENDING: "待確認" };
  const currentAction = saved.action || "UNDECIDED";
  async function save(action: string, extra: Record<string, string> = {}) {
    if (blockedReason) { setFeedback({ error: true, text: blockedReason }); return; }
    setSaving(action);
    setFeedback(null);
    await act(async () => {
      try {
        await api(`/requirements/${row.id}/decision`, "PUT", { action, category, ...extra });
        setEditing(false);
        setFeedback({ error: false, text: `✓ ${decisionLabels[action]}` });
      } catch (e) {
        setFeedback({ error: true, text: `保存失敗：${(e as Error).message}` });
        throw e;
      } finally {
        setSaving("");
      }
    });
  }
  async function organize() {
    if (!note.trim()) { setFeedback({ error: true, text: "請先寫下你想怎麼改。" }); return; }
    setSaving("REWRITE");
    setFeedback(null);
    try {
      const result = await api(`/requirements/${row.id}/rewrite`, "POST", { note, statement });
      setStatement(result.statement);
      setFeedback({ error: false, text: result.warning || (result.method === "MODEL" ? "AI 已整理，請確認後保存。" : "已合併你的說明，請確認後保存。") });
    } catch (e) {
      setFeedback({ error: true, text: (e as Error).message });
    } finally {
      setSaving("");
    }
  }
  const disabled = busy || !!saving || !!blockedReason;
  return (
    <article className={`card requirement decision-${currentAction.toLowerCase()}`} aria-busy={!!saving}>
      <div className="row">
        <span className="decision-badge">{decisionLabels[currentAction] || "尚未決策"}</span>
        {proposal && <span className={`badge change-${String(proposal.status).toLowerCase()}`}>{label(proposal.status)}</span>}
        {criticalReason && <span className="badge change-true_conflict">{label(criticalReason)}</span>}
      </div>
      <h3>{row.topic}</h3>
      {proposal && proposal.current_state && proposal.status !== "ADD" && <p className="muted"><strong>現況：</strong>{proposal.current_state}</p>}
      <p><strong>{saved.action === "MODIFY" ? "修改後需求：" : "AI 建議需求："}</strong>{saved.action === "MODIFY" ? saved.statement : suggestion}</p>
      {saved.action === "MODIFY" && saved.rationale && <p className="muted">修改說明：{saved.rationale}</p>}
      <details>
        <summary>依據 · {row.evidence.length} 筆</summary>
        {row.evidence.map((f: any, i: number) => (
          <div className="evidence" key={i}>
            <small>{f.meeting_date || "文件來源"} · {f.validity || f.kind}</small>
            <p>{f.statement}</p>
            {f.conflict_reason && <p className="execution-warning">{f.conflict_reason}；請對照雙方原文與來源。</p>}
            {sourceButton(f.source_id)}
          </div>
        ))}
      </details>
      {editing && <div className="modify-panel">
        <label>你想怎麼改？（AI 會依此整理成正式需求）
          <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：附件密碼改以簡訊另送，不放在同一封信" />
        </label>
        <div className="actions">
          <button disabled={disabled || !note.trim()} onClick={() => void organize()}>{saving === "REWRITE" ? "整理中…" : "請 AI 整理"}</button>
        </div>
        <label>修改後的需求
          <textarea value={statement} onChange={(e) => setStatement(e.target.value)} />
        </label>
        <label>PRD 內容類型
          <select value={category} disabled={row.status === "EXCLUDED"} onChange={(e) => setCategory(e.target.value)}>
            <option value="FUNCTIONAL">功能需求</option>
            <option value="BUSINESS_RULE">商業規則</option>
            <option value="OPEN_ISSUE">待決事項</option>
            <option value="DECISION_CONTEXT">決策背景、時程或分工</option>
            <option value="SCOPE_EXCLUSION">排除範圍</option>
          </select>
        </label>
        <div className="actions">
          <button className="primary" disabled={disabled || !statement.trim()} onClick={() => void save("MODIFY", { statement, rationale: note })}>保存修改</button>
          <button disabled={!!saving} onClick={() => setEditing(false)}>取消</button>
        </div>
      </div>}
      {blockedReason && <p className="feedback warning">{blockedReason}</p>}
      {saving && saving !== "REWRITE" && <p role="status" className="feedback loading"><span className="spinner" />正在保存…</p>}
      {feedback && !saving && <p role={feedback.error ? "alert" : "status"} className={`feedback ${feedback.error ? "warning" : "success"}`}>{feedback.text}</p>}
      {!editing && <div className="actions">
        <button disabled={disabled} aria-pressed={currentAction === "ACCEPT"} className={`decision-button choice-accept ${currentAction === "ACCEPT" ? "selected" : ""}`} onClick={() => void save("ACCEPT")}>採納</button>
        <button disabled={disabled} aria-pressed={currentAction === "MODIFY"} className={`decision-button choice-modify ${currentAction === "MODIFY" ? "selected" : ""}`} onClick={() => { setStatement(saved.action === "MODIFY" ? saved.statement : suggestion); setEditing(true); }}>修改…</button>
        <button disabled={disabled} aria-pressed={currentAction === "REJECT"} className={`decision-button choice-reject ${currentAction === "REJECT" ? "selected" : ""}`} onClick={() => void save("REJECT")}>拒絕</button>
        <button disabled={disabled} aria-pressed={currentAction === "PENDING"} className={`decision-button choice-pending ${currentAction === "PENDING" ? "selected" : ""}`} onClick={() => void save("PENDING", { category: "OPEN_ISSUE" })}>稍後確認</button>
      </div>}
    </article>
  );
}

function ChangeCard({
  change,
  busy,
  act,
}: {
  change: any;
  busy: boolean;
  act: Action;
}) {
  const [rationale, setRationale] = useState("");
  return (
    <article className="card">
      <div className="row">
        <h3>{change.summary}</h3>
        <span className="badge">{label(change.status)}</span>
      </div>
      <p>
        影響需求：{change.impact.affected_requirement_ids?.length ?? "待分析"}{" "}
        項
      </p>
      <p className="muted">
        影響章節：
        {change.impact.affected_prd_sections?.join("、") || "請先繼續分析"}
      </p>
      {["OPEN", "REVIEWED"].includes(change.status) && (
        <>
          <input
            aria-label="變更決策理由"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder="請填寫變更決策理由"
          />
          <div className="actions">
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                act(() =>
                  api("/change-requests/" + change.id + "/resolve", "POST", {
                    action: "REVISION_REQUIRED",
                    rationale,
                  }),
                )
              }
            >
              核准修訂
            </button>
            <button
              disabled={busy}
              onClick={() =>
                act(() =>
                  api("/change-requests/" + change.id + "/resolve", "POST", {
                    action: "NO_PRD_CHANGE",
                    rationale,
                  }),
                )
              }
            >
              無須改版
            </button>
          </div>
        </>
      )}
    </article>
  );
}

function DiagramEditor({
  diagram,
  index,
  revision,
  act,
}: {
  diagram: any;
  index: number;
  revision: any;
  act: Action;
}) {
  const [value, setValue] = useState(diagram.mermaid);
  return (
    <article className="card">
      <h3>{diagram.title}</h3>
      <p className="muted">{diagram.warning}</p>
      <Diagram source={diagram.mermaid} />
      <details>
        <summary>檢視／編輯 Mermaid</summary>
        <textarea
          className="code"
          rows={8}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        {revision.status === "IN_REVIEW" && (
          <button
            onClick={() =>
              act(() =>
                api(`/prd-revisions/${revision.id}/diagrams/${index}`, "PUT", {
                  mermaid: value,
                  title: diagram.title,
                }),
              )
            }
          >
            保存圖表來源
          </button>
        )}
      </details>
    </article>
  );
}

function MeetingCard({
  meeting: m,
  busy,
  act,
}: {
  meeting: any;
  busy: boolean;
  act: Action;
}) {
  const [transcript, setTranscript] = useState(m.transcript);
  const [manualNotes, setManualNotes] = useState("");
  const [feedback, setFeedback] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<number[]>([]);
  const eventSignature = JSON.stringify(m.events);
  useEffect(() => setSelectedEvents(m.events.map((_: unknown, index: number) => index)), [eventSignature]);
  useEffect(() => setTranscript(m.transcript), [m.transcript]);
  const names: Record<string, string> = {
    IMPORTED: "已保存 · 待分析",
    ANALYZING: "正在分析會議",
    ANALYZED: "分析完成 · 等待確認",
    NO_FINDINGS: "內容不足 · 請補充",
    CONFIRMED: "已確認 · 已加入證據",
    ERROR: "分析失敗 · 可重試",
  };
  const working = m.status === "ANALYZING";
  async function action(fn: () => Promise<unknown>, message: string) {
    setFeedback("");
    await act(async () => {
      try {
        await fn();
        setFeedback(message);
      } catch (error) {
        setFeedback((error as Error).message);
        throw error;
      }
    });
  }
  return (
    <article className="card timeline-card meeting-card" data-state={m.status}>
      <div className="row">
        <span className="meeting-date">{m.meeting_date}</span>
        <span className="meeting-state" role="status">
          {working && <span className="spinner" />}
          {names[m.status] || m.status}
        </span>
      </div>
      <h3>{m.title}</h3>
      <p className="muted">{m.participants.join("、") || "未填寫參與者"}</p>
      <div className="meeting-progress">
        <span className="complete">1 保存逐字稿</span>
        <span
          className={m.events.length ? "complete" : working ? "current" : ""}
        >
          2 分析與整理
        </span>
        <span
          className={
            m.status === "CONFIRMED"
              ? "complete"
              : m.status === "ANALYZED"
                ? "current"
                : ""
          }
        >
          3 人工確認
        </span>
      </div>
      {working && (
        <div className="feedback loading" aria-live="polite">
          正在呼叫模型整理決議、變更與待辦。你可以繼續操作，結果會自動更新。
        </div>
      )}
      {m.analysis_error && (
        <div className="feedback warning" role="alert">
          {m.analysis_error}
        </div>
      )}
      {feedback && (
        <div className="feedback" role="status">
          {feedback}
        </div>
      )}
      {m.status === "CONFIRMED" && (
        <div className="feedback success">
          ✓ 已確認 {m.events.length} 項紀錄並加入專案證據。可回到 AI
          分析，更新需求或建立修訂。
        </div>
      )}
      <details open={m.status === "NO_FINDINGS" || m.status === "ERROR"}>
        <summary>
          {m.status === "CONFIRMED" ? "查看原始逐字稿" : "檢視／補編逐字稿"}
        </summary>
        {m.status === "CONFIRMED" ? (
          <p className="prewrap">{m.transcript}</p>
        ) : (
          <>
            <label>
              會議逐字稿
              <textarea
                rows={5}
                value={transcript}
                onChange={(e) => setTranscript(e.target.value)}
                placeholder="貼上實際討論內容，例如：入帳帳戶：決議僅支援本人本行帳戶。"
              />
            </label>
            <div className="actions">
              <button
                disabled={busy || !transcript.trim()}
                onClick={() =>
                  action(
                    () =>
                      api(`/meetings/${m.id}/transcript`, "PATCH", {
                        text: transcript,
                      }),
                    "逐字稿已保存，請重新分析。",
                  )
                }
              >
                保存逐字稿
              </button>
              <label className="file-label">
                匯入 TXT／VTT
                <input
                  type="file"
                  accept=".txt,.vtt,.md"
                  disabled={busy}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file)
                      action(async () => {
                        const form = new FormData();
                        form.append("file", file);
                        await api(`/meetings/${m.id}/transcript`, "POST", form);
                      }, "逐字稿已讀取，請執行分析。");
                  }}
                />
              </label>
            </div>
          </>
        )}
      </details>
      {m.events.length > 0 && (
        <div className="meeting-events">
          <h4>請審閱這 {m.events.length} 項會議紀錄</h4>
          {m.status !== "CONFIRMED" && <p className="muted">勾選確實出自會議、應納入本案證據的項目。未勾選項目不會進入需求分析。</p>}
          {m.events.map((event: any, i: number) => (
            <div className={`meeting-event ${m.status !== "CONFIRMED" && !selectedEvents.includes(i) ? "excluded" : ""}`} key={i}>
              {m.status !== "CONFIRMED" && <input type="checkbox" aria-label={`納入會議紀錄 ${i + 1}：${event.statement}`} checked={selectedEvents.includes(i)} onChange={(e) => setSelectedEvents((old) => e.target.checked ? [...old, i].sort((a, b) => a - b) : old.filter((value) => value !== i))} />}
              <span className="badge">
                {(
                  {
                    DECISION: "決議",
                    CHANGE: "變更",
                    OPEN_ITEM: "待釐清",
                    ACTION_ITEM: "待辦",
                    REQUIREMENT: "需求",
                    QUESTION: "提問",
                    EXCLUDED: "不納入",
                    SYSTEM_CAPABILITY: "系統能力",
                  } as Record<string, string>
                )[event.kind] || event.kind}
              </span>
              <p>{event.statement}</p>
              {event.origin === "HUMAN" && <small>人工整理</small>}
            </div>
          ))}
        </div>
      )}
      {m.status !== "CONFIRMED" && (
        <details>
          <summary>人工整理紀錄（不必等待模型）</summary>
          <p className="muted">
            每行一項，請填入你確認過的決議、變更或待辦。保存後仍需按下最後確認。
          </p>
          <label>
            人工會議紀錄
            <textarea
              rows={4}
              value={manualNotes}
              onChange={(e) => setManualNotes(e.target.value)}
              placeholder="入帳帳戶：決議只支援本人本行帳戶。&#10;每日限額：仍待風控確認。"
            />
          </label>
          <button
            disabled={busy || !manualNotes.trim()}
            onClick={() =>
              action(
                () =>
                  api(`/meetings/${m.id}/manual-record`, "POST", {
                    text: manualNotes,
                  }),
                "人工紀錄已保存，請審閱後確認。",
              )
            }
          >
            保存人工紀錄
          </button>
        </details>
      )}
      <div className="actions">
        {m.status !== "CONFIRMED" && (
          <>
            <button
              disabled={busy || working || !transcript.trim()}
              onClick={() =>
                action(async () => {
                  if (transcript !== m.transcript)
                    await api(`/meetings/${m.id}/transcript`, "PATCH", {
                      text: transcript,
                    });
                  await api(`/meetings/${m.id}/analyze`, "POST");
                }, "已開始分析，狀態將自動更新。")
              }
            >
              {working
                ? "分析中…"
                : m.events.length
                  ? "重新分析"
                  : "分析會議紀錄"}
            </button>
            <button
              className="primary"
              disabled={busy || m.status !== "ANALYZED" || !selectedEvents.length}
              onClick={() =>
                action(
                  () => api(`/meetings/${m.id}/confirm`, "POST", { selected_indices: selectedEvents }),
                  "確認成功，已加入專案證據。",
                )
              }
            >
              確認選取的 {selectedEvents.length} 項紀錄
            </button>
          </>
        )}
        <a className="button" href={`/api/meetings/${m.id}/ics`}>
          下載行事曆
        </a>
      </div>
      {m.status !== "CONFIRMED" && m.events.length === 0 && !working && (
        <p className="muted confirm-hint">
          先分析或人工整理出紀錄，確認按鈕才會啟用。
        </p>
      )}
    </article>
  );
}

function App({ isClient = false }: { isClient?: boolean }) {
  const [leftCollapsed, setLeftCollapsed] = useState(() => localStorage.getItem("tongling-left-collapsed") === "1");
  const [rightCollapsed, setRightCollapsed] = useState(() => {
    const saved = localStorage.getItem("tongling-right-collapsed");
    return saved === null ? window.matchMedia("(max-width: 1360px)").matches : saved === "1";
  });
  const [mobileLeftOpen, setMobileLeftOpen] = useState(false);
  const [mobileRightOpen, setMobileRightOpen] = useState(false);
  const lastMobileSide = useRef<"left" | "right" | null>(null);
  useEffect(() => {
    if (mobileLeftOpen) {
      lastMobileSide.current = "left";
      document.querySelector<HTMLElement>("#project-sidebar .left-toggle")?.focus();
    } else if (mobileRightOpen) {
      lastMobileSide.current = "right";
      document.querySelector<HTMLElement>("#work-sidebar .side-toggle")?.focus();
    } else if (lastMobileSide.current) {
      document.querySelector<HTMLElement>(lastMobileSide.current === "left" ? ".header-left-toggle" : ".header-right-toggle")?.focus();
      lastMobileSide.current = null;
    }
  }, [mobileLeftOpen, mobileRightOpen]);
  useEffect(() => { localStorage.setItem("tongling-left-collapsed", leftCollapsed ? "1" : "0"); }, [leftCollapsed]);
  useEffect(() => { localStorage.setItem("tongling-right-collapsed", rightCollapsed ? "1" : "0"); }, [rightCollapsed]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setMobileLeftOpen(false); setMobileRightOpen(false); }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  function toggleLeft() {
    if (window.matchMedia("(max-width: 820px)").matches) {
      setMobileRightOpen(false);
      setMobileLeftOpen((open) => !open);
    } else setLeftCollapsed((collapsed) => !collapsed);
  }
  function toggleRight() {
    if (window.matchMedia("(max-width: 820px)").matches) {
      setMobileLeftOpen(false);
      setMobileRightOpen((open) => !open);
    } else setRightCollapsed((collapsed) => !collapsed);
  }
  function clickFeedback(event: React.MouseEvent<HTMLDivElement>) {
    const target = (event.target as HTMLElement).closest<HTMLElement>("button:not(:disabled), .button, summary");
    if (!target) return;
    target.classList.remove("click-pop");
    void target.offsetWidth;
    target.classList.add("click-pop");
    window.setTimeout(() => target.classList.remove("click-pop"), 420);
  }
  const [customProjection, setCustomProjection] = useState<any>(null);
  const [prdAdvice, setPrdAdvice] = useState<any>(null);
  const [enrichmentJobs, setEnrichmentJobs] = useState<any[]>([]);
  async function projectsChanged(deleted?: string) {
    const list = await api<any[]>("/projects");
    setProjects(list);
    if (isClient) setTrashed(await api<any[]>("/projects/trash"));
    if (deleted === pid || !pid) {
      setProject(null);
      setPid(list[0]?.id || "");
      if (deleted) setHomeOpen(true);
    }
  }
  const [lastSync, setLastSync] = useState(0);
  const [syncError, setSyncError] = useState("");
  const [globalTasks, setGlobalTasks] = useState<any[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => {
    let active = true;
    const refreshJobs = () => {
      if (document.visibilityState !== "visible") return;
      api<Job[]>("/jobs").then((rows) => { if (active) setJobs(rows); }).catch(() => {});
    };
    refreshJobs();
    const timer = window.setInterval(refreshJobs, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  async function dismissJob(id: string) {
    try {
      updateJob(await api<Job>(`/jobs/${id}/dismiss`, "POST"));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function dismissActivity(id: string) {
    try {
      await api(`/activities/${id}/dismiss`, "POST");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function updateJob(job: Job) {
    setJobs((current) => [{ ...job, result: undefined }, ...current.filter((row) => row.job_id !== job.job_id)].slice(0, 30));
  }
  function queueAndWait(kind: "GUIDANCE" | "RAG" | "PRD", payload: Record<string, unknown>) {
    return submitAndWait(kind, payload, updateJob);
  }
  const [projects, setProjects] = useState<any[]>([]),
    [pid, setPid] = useState(""),
    [project, setProject] = useState<any>(null);
  const [trashed, setTrashed] = useState<any[]>([]);
  const [page, setPage] = useState(1),
    [config, setConfig] = useState<any>(null),
    [settingsOpen, setSettingsOpen] = useState(false);
  const [homeOpen, setHomeOpen] = useState(() => !location.hash.slice(1) || location.hash === "#hub");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false),
    [name, setName] = useState(""),
    [brief, setBrief] = useState("");
  const [guidance, setGuidance] = useState<any>(null);
  const [guidanceBusy, setGuidanceBusy] = useState(false);
  const [guidanceSeconds, setGuidanceSeconds] = useState(0);
  const [guidanceError, setGuidanceError] = useState("");
  useEffect(() => {
    if (!guidanceBusy) return;
    const timer = window.setInterval(() => setGuidanceSeconds((seconds) => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, [guidanceBusy]);
  const [delegated, setDelegated] = useState(false);
  const [startOnCreate, setStartOnCreate] = useState(true);
  const [initialMode, setInitialMode] = useState<"AGENT" | "MANUAL">("AGENT");
  const [composer, setComposer] = useState(""),
    [showActivity, setShowActivity] = useState(false),
    [interrupt, setInterrupt] = useState<any>(null);
  const [source, setSource] = useState<any>(null),
    [query, setQuery] = useState(""),
    [results, setResults] = useState<any[]>([]);
  const [searchWarnings, setSearchWarnings] = useState<string[]>([]);
  const [searchSummary, setSearchSummary] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchSeconds, setSearchSeconds] = useState(0);
  const [searchProjectId, setSearchProjectId] = useState("");
  const [suggestedQueries, setSuggestedQueries] = useState<string[]>([]);
  const [suggestionError, setSuggestionError] = useState("");
  useEffect(() => {
    setQuery("");
    setSuggestedQueries([]);
    setSuggestionError("");
  }, [pid]);
  useEffect(() => {
    if (!pid || page !== 2) return;
    let active = true;
    api<{ queries: string[] }>(`/projects/${pid}/retrieval-suggestions`)
      .then((data) => { if (active) { setSuggestedQueries(data.queries); setSuggestionError(""); } })
      .catch((e) => { if (active) setSuggestionError(`建議詞暫時無法讀取：${e.message}`); });
    return () => { active = false; };
  }, [pid, page, project?.description, project?.sources?.length]);
  useEffect(() => {
    if (!searching) return;
    const timer = window.setInterval(() => setSearchSeconds((seconds) => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, [searching]);
  const [meetingForm, setMeetingForm] = useState({
    title: "",
    meeting_date: "",
    started_at: "",
    participants: "",
    transcript: "",
  });
  const [connectors, setConnectors] = useState<any[]>([]),
    [folder, setFolder] = useState(""),
    [kind, setKind] = useState("USER_UPLOAD");
  const [revisionId, setRevisionId] = useState("");
  const [deliveryCheck, setDeliveryCheck] = useState<any>(null);
  const [showItReference, setShowItReference] = useState(() => localStorage.getItem("tongling-it-gap-reference") === "1");
  useEffect(() => { localStorage.setItem("tongling-it-gap-reference", showItReference ? "1" : "0"); }, [showItReference]);
  const [folderConfig, setFolderConfig] = useState<any>(null);
  const [settingsTab, setSettingsTab] = useState<string>("model");
  const [workspacePath, setWorkspacePath] = useState("");
  const [folderStatus, setFolderStatus] = useState("");
  async function folderAction(action: "save" | "scan" | "samples") {
    setFolderStatus(
      action === "scan" ? "正在讀取資料夾並解析新增文件…" : "正在處理…",
    );
    await act(async () => {
      try {
        if (folderConfig?.can_edit)
          await api("/workspace/folder", "PUT", { path: workspacePath });
        const result =
          action === "scan"
            ? await api("/workspace/folder/scan", "POST")
            : action === "samples"
              ? await api("/demo/install-files", "POST")
              : {
                  message:
                    "專用資料夾已保存。可把文件放入這個資料夾，再按「讀取資料夾」。",
                };
        const saved = await api("/workspace/folder");
        setFolderConfig(saved);
        setWorkspacePath(saved.path);
        setFolderStatus(
          result.message +
            (result.errors?.length
              ? " " +
                result.errors
                  .map((e: any) => `${e.file}：${e.error}`)
                  .join("；")
              : ""),
        );
        setConnectors(await api("/connectors"));
      } catch (error) {
        setFolderStatus((error as Error).message);
        throw error;
      }
    });
  }
  async function applyDemo() {
    await act(async () => {
      if (folderConfig?.can_edit && workspacePath.trim())
        await api("/workspace/folder", "PUT", { path: workspacePath });
      const result = await api("/demo/apply", "POST");
      await api(`/projects/${result.project_id}/workflow/mode`, "POST", {
        mode: "MANUAL",
      });
      setProjects(await api("/projects"));
      setPid(result.project_id);
      setHomeOpen(false);
      setPage(4);
      setSettingsOpen(false);
      setNotice(result.message);
      const saved = await api("/workspace/folder");
      setFolderConfig(saved);
      setWorkspacePath(saved.path);
    });
  }
  const [modelTest, setModelTest] = useState<{
    state: "loading" | "success" | "error";
    message: string;
  } | null>(null);
  const [testSeconds, setTestSeconds] = useState(0);
  useEffect(() => {
    if (modelTest?.state !== "loading") return;
    setTestSeconds(0);
    const timer = setInterval(() => setTestSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [modelTest?.state]);
  async function testModel(generation = false) {
    setBusy(true);
    setModelTest({
      state: "loading",
      message: generation
        ? "正在測試模型生成與證據引用，請稍候（最多約 70 秒）…"
        : "正在保存設定並檢查連線與模型清單（通常數秒）…",
    });
    try {
      await api("/settings", "PUT", config, 10000);
      const result = await api(
        generation
          ? "/settings/providers/test-generation"
          : "/settings/providers/test",
        "POST",
        undefined,
        generation ? 75000 : 15000,
      );
      setModelTest({
        state: "success",
        message: `${result.message} · ${(result.latency_ms / 1000).toFixed(2)} 秒`,
      });
    } catch (error) {
      setModelTest({ state: "error", message: (error as Error).message });
    } finally {
      setBusy(false);
    }
  }
  const uploadRef = useRef<HTMLInputElement>(null);
  const selectedProject = useRef("");
  selectedProject.current = pid;
  async function refresh(id = pid) {
    if (!id) return undefined;
    let p;
    try {
      p = await api("/projects/" + id, "GET", undefined, 10000);
    } catch (e) {
      if (selectedProject.current === id)
        setSyncError(
          "無法取得最新狀態；畫面保留上次資料，不能據此判定 Agent 仍在工作。" +
            (e as Error).message,
        );
      throw e;
    }
    if (selectedProject.current !== id) return undefined;
    setLastSync(Date.now());
    setSyncError("");
    setProject(p);
    return p;
  }
  async function load() {
    const [ps, cs, ss, fs] = await Promise.all([
      api<any[]>("/projects"),
      isClient ? Promise.resolve([]) : api<any[]>("/connectors"),
      api(isClient ? "/client/workspace-config" : "/settings"),
      isClient ? Promise.resolve(null) : api("/workspace/folder"),
    ]);
    setProjects(ps);
    if (isClient) setTrashed(await api<any[]>("/projects/trash"));
    setConnectors(cs);
    setConfig(ss);
    setFolderConfig(fs);
    if (fs) setWorkspacePath(fs.path);
    if (!pid && ps[0]) {
      const saved = location.hash.slice(1);
      setPid(ps.some((p) => p.id === saved) ? saved : ps[0].id);
    }
  }
  useEffect(() => {
    if (isClient) return;
    let stopped = false;
    let pending = false;
    async function pollTasks() {
      if (pending) return;
      pending = true;
      try {
        const tasks = await api<any[]>("/activities", "GET", undefined, 10000);
        if (!stopped) {
          setGlobalTasks(tasks);
          setEnrichmentJobs(await api<any[]>("/knowledge/jobs"));
        }
      } catch {
        /* project connection banner and heartbeat age show stale state */
      } finally {
        pending = false;
      }
    }
    void pollTasks();
    const timer = setInterval(pollTasks, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [isClient]);
  useEffect(() => {
    if (!homeOpen && !pid) return;
    history.replaceState(null, "", homeOpen ? "#hub" : pid ? "#" + pid : "#hub");
  }, [pid, homeOpen]);
  async function act(fn: () => Promise<unknown>, message = "", skipRefresh = false) {
    setBusy(true);
    setError("");
    try {
      await fn();
      if (message) setNotice(message);
      if (!skipRefresh) await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!pid) return;
    let stopped = false;
    let pending = false;
    let lastPoll = 0;
    let latestRuntime = "READY";
    let knownEventId = 0;
    setProject(null);
    setInterrupt(null);
    setRevisionId("");
    async function pulse(force = false) {
      if (pending || stopped) return;
      const interval = document.visibilityState === "hidden" ? 30000 : latestRuntime === "RUNNING" ? 2000 : 10000;
      if (!force && Date.now() - lastPoll < interval) return;
      pending = true;
      lastPoll = Date.now();
      try {
        const status = await api<any>(`/projects/${pid}/pulse`, "GET", undefined, 10000);
        if (stopped || selectedProject.current !== pid) return;
        const previousRuntime = latestRuntime;
        latestRuntime = status.runtime;
        setLastSync(Date.now());
        setSyncError("");
        setProject((current: any) => current?.id === pid ? {
          ...current,
          runtime: status.runtime,
          state: { ...current.state, ...status.state },
          epoch: status.epoch,
          latest_run: status.latest_run,
          activities: status.activities,
        } : current);
        if (status.last_event_id > knownEventId || (previousRuntime === "RUNNING" && status.runtime !== "RUNNING")) {
          knownEventId = status.last_event_id;
          const full = await refresh(pid);
          if (full && !stopped) knownEventId = Math.max(knownEventId, Number(full.events?.at(-1)?.id || 0));
        }
      } catch (e) {
        if (!stopped && selectedProject.current === pid) setSyncError("無法取得最新狀態；畫面保留上次資料，不能據此判定 Agent 仍在工作。" + (e as Error).message);
      } finally {
        pending = false;
      }
    }
    refresh(pid).then((full) => {
      if (stopped || !full) return;
      latestRuntime = full.runtime;
      knownEventId = Number(full.events?.at(-1)?.id || 0);
      void pulse(true);
    }).catch((e) => setError(e.message));
    const timer = setInterval(() => { void pulse(); }, 2000);
    const visibleAgain = () => { if (document.visibilityState === "visible") void pulse(true); };
    document.addEventListener("visibilitychange", visibleAgain);
    return () => {
      stopped = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visibleAgain);
    };
  }, [pid]);
  // Pending items show inline (next-step bar, Agent card); the dialog opens only on request.
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(""), 5000);
    return () => clearTimeout(t);
  }, [notice]);
  const manual = project?.mode === "MANUAL";
  const [detailView, setDetailView] = useState(false);
  const showSteps = manual || detailView;
  function openStep(step: number) {
    setDetailView(true);
    setPage(step);
  }
  function showSearchResult(response: any, projectId: string) {
    setSearchProjectId(projectId);
    setResults(response.results || []);
    setSearchWarnings(response.warnings || []);
    const count = response.results?.length || 0;
    setSearchSummary(`檢索完成：本機 ${response.local_count || 0} 筆、共用知識伺服器 ${response.remote_count || 0} 筆。${count ? "請檢查相關性，再加入專案依據。" : "沒有符合的來源，可調整檢索詞或確認知識庫範圍。"}${response.queries?.length ? ` 本次檢索詞：${response.queries.join("、")}` : ""}`);
  }
  async function searchKnowledge(automatic = false, term = query) {
    const projectId = pid;
    setSearching(true);
    setSearchSeconds(0);
    setSearchProjectId(projectId);
    setResults([]);
    setSearchWarnings([]);
    setSearchSummary("");
    try {
      const response = await queueAndWait("RAG", { query: term, project_id: projectId, automatic });
      if (selectedProject.current === projectId) showSearchResult(response, projectId);
    } finally {
      setSearching(false);
    }
  }
  const attachedIds = new Set((project?.sources || []).map((src: any) => src.id));
  const attachedRemote = new Set((project?.sources || []).map((src: any) => src.meta?.remote_key).filter(Boolean));
  const excludedHits = (project?.state?.retrieval_results || []).filter((hit: any) =>
    !["KEY", "SUPPORTING"].includes(hit.evidence_role) && !attachedIds.has(hit.source_id) &&
    !attachedRemote.has(`${hit.provider_id}:${hit.remote_evidence_id}`) && hit.title !== "需求起點");
  const pmEvents = (project?.events || []).filter((e: any) => !HIDDEN_EVENTS.has(e.kind));
  const nextStep = (() => {
    if (!project) return null;
    const report = project.state.change_analysis;
    const reviewed = report && project.state.change_review?.status === "APPROVED" && project.state.change_review?.fingerprint === report.fingerprint;
    const open = report ? report.proposals.filter((item: any) => (item.needs_review ?? true) &&
      !["ACCEPT", "MODIFY", "REJECT"].includes(project.requirements.find((r: any) => r.id === item.requirement_id)?.decision?.action)).length : 0;
    if (project.runtime === "RUNNING") return { text: "彤靈正在處理，完成後會告訴你下一步。", step: 0 };
    if (!project.sources.length) return { text: "先描述需求或上傳需求文件。", step: 1 };
    if (project.state.analysis_stale || !report) return { text: "依據已更新，請執行需求分析。", step: 5, action: "analyze" };
    if (!reviewed) return { text: open ? `有 ${open} 項變更提案待你審閱。` : "提案已整理完成，請確認整體提案。", step: 6 };
    if (!project.state.draft_id && !project.current_revision_id) return { text: "提案已審閱，可以產生 PRD 草稿。", step: 6 };
    if (project.state.draft_id) return { text: "PRD 草稿已產生，請預覽並確認定版。", step: 8 };
    return { text: "已有正式版本；加入新資訊即可開始下一次修訂。", step: 9 };
  })();
  const workActive = busy || project?.runtime === "RUNNING" ||
    jobs.some((job) => job.status === "QUEUED" || job.status === "RUNNING") ||
    project?.activities?.some((task: any) => ["RUNNING", "QUEUED"].includes(task.status)) ||
    enrichmentJobs.some((job) => ["RUNNING", "QUEUED", "WAITING_RETRY", "PAUSING"].includes(job.status));
  const revision =
    project?.revisions?.find((r: any) => r.id === revisionId) ||
    project?.revisions?.find((r: any) => r.id === project.state.draft_id) ||
    project?.revisions?.[0];
  const current = project?.revisions?.find(
    (r: any) => r.id === project.current_revision_id,
  );
  useEffect(() => {
    if (!revision?.id) { setPrdAdvice(null); return; }
    let live = true;
    const id = revision.id;
    api<any>(`/prd-revisions/${id}/assist/prd`)
      .then((result) => { if (live) setPrdAdvice(result ? { ...result, revision_id: id } : null); })
      .catch(() => { if (live) setPrdAdvice(null); });
    return () => { live = false; };
    // Keyed to the draft, not its hash: adopting a suggestion edits the draft.
  }, [revision?.id]);
  useEffect(() => {
    if (page !== 7 || !revision?.id || !showItReference) { setDeliveryCheck(null); return; }
    let live = true;
    api(`/prd-revisions/${revision.id}/delivery-readiness`)
      .then((result) => { if (live) setDeliveryCheck(result); })
      .catch((e) => { if (live) setDeliveryCheck({ readiness: "ERROR", message: e.message, blocking_gaps: [] }); });
    return () => { live = false; };
  }, [page, revision?.id, revision?.content_hash, showItReference]);
  async function switchMode() {
    if (project.runtime === "RUNNING" &&
      !window.confirm("切換工作方式會暫停目前的 Agent 工作（已完成的成果會保留）。確定切換？")) return;
    await api("/projects/" + pid + "/workflow/mode", "POST", {
      mode: manual ? "AGENT" : "MANUAL",
    });
    if (!manual) setPage(project.state.step || 1);
    setDetailView(false);
  }
  async function continueRun() {
    await api("/projects/" + pid + "/agent/continue", "POST");
  }
  async function upload(file: File, library = false) {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    await api(
      library ? "/knowledge/upload" : "/projects/" + pid + "/sources/upload",
      "POST",
      form,
    );
  }
  async function goReview(i: any) {
    await api("/interruptions/" + i.id + "/dismiss", "POST");
    setInterrupt(null);
    await api("/projects/" + pid + "/workflow/mode", "POST", {
      mode: "MANUAL",
    });
    setPage(
      i.kind === "FINAL_PRD_REVIEW_REQUIRED"
        ? 8
        : i.kind === "CHANGE_REVIEW"
          ? 9
          : i.kind === "WAITING_MEETING"
            ? 4
            : i.kind === "REQUIREMENT_INPUT_REQUIRED"
              ? 1
              : 6,
    );
  }
  function sourceButton(id: string) {
    return (
      <button
        className="text-button"
        onClick={() => act(async () => setSource(await api("/sources/" + id + (isClient ? `?project_id=${encodeURIComponent(pid)}` : ""))))}
      >
        查看證據原文 ↗
      </button>
    );
  }

  const sourcesView = (
    <>
      <div className="section-head">
        <div>
          <h2>本案依據</h2>
        </div>
      </div>
      <div className="card">
        <div className="row">
          <select
            aria-label="來源種類"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            {[
              ["USER_UPLOAD", "一般文件"],
              ["INTERNAL_POLICY", "內部規範"],
              ["TECH_SPEC", "技術規格／SA"],
              ["COMPETITOR", "競品參考"],
              ["HISTORICAL_PRD", "歷史 PRD"],
            ].map(([v, l]) => (
              <option value={v} key={v}>
                {l}
              </option>
            ))}
          </select>
          <button
            className="primary"
            onClick={() => uploadRef.current?.click()}
            disabled={busy}
          >
            ＋ 上傳文件
          </button>
        </div>
        <p className="muted">
          PDF、Word、PowerPoint、Excel、CSV、TXT、Markdown、VTT · 每檔 30 MB
        </p>
        {!isClient && <div className="row">
          <input
            aria-label="本機資料夾路徑"
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="Runtime 主機上的資料夾完整路徑"
          />
          <button
            disabled={busy}
            onClick={() =>
              act(async () => {
                const c = await api("/connectors", "POST", {
                  path: folder,
                  project_id: pid,
                  name: "專案資料夾",
                });
                const report = await api(
                  "/connectors/" + c.id + "/scan",
                  "POST",
                );
                setNotice(JSON.stringify(report));
                setConnectors(await api("/connectors"));
                setFolder("");
              })
            }
          >
            連接資料夾
          </button>
        </div>}
        {!isClient && connectors
          .filter((c) => c.project_id === pid)
          .map((c) => (
            <div className="row" key={c.id}>
              <span>
                {c.name} · {c.files} 檔案
              </span>
              <button
                onClick={() =>
                  act(async () => {
                    const result = await api(
                      "/connectors/" + c.id + "/scan",
                      "POST",
                    );
                    setNotice(
                      `新增／異動 ${result.changed}，移除 ${result.removed}，錯誤 ${result.errors.length}`,
                    );
                    if (result.errors.length)
                      setError(JSON.stringify(result.errors));
                  })
                }
              >
                增量重新掃描
              </button>
            </div>
          ))}
      </div>
      <div className="cards">
        {project?.sources.length === 0 && <p className="muted">尚未加入依據。可在上一步輸入需求、上傳文件，或到「知識檢索」加入共用知識。</p>}
        {project?.sources.map((s: any) => (
          <article className="card" key={s.id}>
            <span className="badge">{SOURCE_KIND_LABELS[s.kind] || s.kind}</span>
            <h3>{s.title}</h3>
            <p className="clamp">{s.content}</p>
            <div className="row">
              <small>
                {s.provider === "KNOWLEDGE_SERVER" ? `公司檔案服務器${s.meta?.excerpt_only ? "（片段）" : ""}` : s.meta?.library_scope === "GLOBAL" ? "共用知識庫" : "本案文件"}
              </small>
              {sourceButton(s.id)}
              <button
                className="text-button danger-text"
                disabled={busy || project.runtime === "RUNNING"}
                title={project.runtime === "RUNNING" ? "Agent 執行中，請先暫停" : "從本案移除，共用知識庫原文不會刪除"}
                onClick={() => {
                  if (!window.confirm(`從本案移除「${s.title}」？由它產生的需求證據會一併撤下；共用知識庫原文不會刪除。`)) return;
                  void act(async () => {
                    const result = await api(`/projects/${pid}/sources/${s.id}`, "DELETE");
                    setNotice(result.message);
                  });
                }}
              >
                移除依據
              </button>
            </div>
          </article>
        ))}
      </div>
    </>
  );

  function renderSingle(which: number) {
    if (which === 1)
      return (
        <>
          <h2>從一個清楚的需求開始</h2>
          <p>輸入目標、使用者、範圍與已知限制，或上傳需求文件。</p>
          <div className="card">
            <h3>{project.name}</h3>
            <p className="prewrap">
              {project.description || "尚未輸入文字，可直接上傳需求文件。"}
            </p>
            <textarea
              rows={7}
              value={composer}
              onChange={(e) => setComposer(e.target.value)}
              placeholder="補充你的需求與背景…"
            />
            <div className="actions">
              <button
                className="primary"
                disabled={!composer.trim() || busy}
                onClick={() =>
                  act(async () => {
                    await api(
                      "/projects/" + pid + "/requirement-text",
                      "POST",
                      { text: composer },
                    );
                    setComposer("");
                  })
                }
              >
                保存需求
              </button>
              <button onClick={() => uploadRef.current?.click()}>
                上傳需求文件
              </button>

            </div>
          </div>
        </>
      );
    if (which === 2)
      return (
        <>
          <h2>搜尋共用知識</h2>
          <details className="knowledge-drawer">
            <summary>標籤、圖譜與同業研究</summary>
            <KnowledgePanel pid={pid} config={config} act={act} isClient={isClient} />
          </details>
          <p>搜尋結果只是參考，加入專案後才會參與需求分析。</p>
          <div className="card">
            <h3>依需求自動尋找知識</h3>
            <p>彤靈會從專案目標與已保存的需求整理檢索詞，搜尋目前允許的本機與共用知識來源，合併重複結果。</p>
            <div className="actions">
              <button className="primary" disabled={busy || searching} onClick={() => act(() => searchKnowledge(true))}>
                {searching ? "檢索中…" : "依需求自動檢索"}
              </button>
            </div>
            {suggestedQueries.length > 0 && <div className="retrieval-suggestions">
              <p className="muted">建議檢索詞 · 點選可單獨搜尋，也可在下方自行調整</p>
              <div className="actions">{suggestedQueries.map((term) => <button key={term} disabled={busy || searching} onClick={() => { setQuery(term); void act(() => searchKnowledge(false, term)); }}>{term}</button>)}</div>
            </div>}
            {suggestionError && <p role="status" className="feedback warning">{suggestionError}</p>}
            <div className="row">
              <input
                aria-label="搜尋知識"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="輸入主題或關鍵字"
              />
              <button
                className="primary"
                disabled={busy || !query.trim()}
                onClick={() => act(() => searchKnowledge())}
              >
                搜尋
              </button>
            </div>
            {searching && (
              <div className="execution-card" role="status" aria-live="polite">
                <strong>正在檢索 · 已等待 {searchSeconds} 秒</strong>
                <p>
                  <span className="spinner" />
                  {config.retrieval_version === "v2"
                    ? "正在比對文件與語意向量。首次使用新模型需建立快取，可能耗時較久；完成後會顯示結果。"
                    : "正在搜尋知識庫，完成後會顯示結果。"}
                </p>
              </div>
            )}
            {!isClient && <label className="file-label">
              ＋ 上傳到共用知識庫
              <input
                type="file"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) act(() => upload(f, true), "知識已加入");
                  e.target.value = "";
                }}
              />
            </label>}
          </div>
          {searchProjectId === pid && searchWarnings.map((warning, i) => <p key={i} role="status" className="feedback warning">{warning}</p>)}
          {searchProjectId === pid && searchSummary && <p role="status" className={`feedback ${results.length ? "success" : "warning"}`}>{searchSummary}</p>}
          {searchProjectId === pid && results.map((r: any) => (
            <article className="card" key={r.chunk_logical_id || r.chunk_id}>
              <span className="badge">
                {label(r.evidence_role)} · 相關度{" "}
                {Math.round((r.case_relevance_score || 0) * 100)}%
              </span>
              <p>
                <small>
                  {r.retrieval_channels?.map(label).join(" + ")} · {label(r.authority_class)} ·{" "}
                  {label(r.temporal_status)}
                </small>
              </p>
              <p>{r.case_relevance_reason}</p>
              <p><small>來源：{r.source_scope === "SERVER_SHARED" ? "共用知識伺服器" : r.source_scope === "LOCAL_PRIVATE" ? "本機知識庫" : r.source_scope === "EXTERNAL_RESEARCH" ? "外部研究" : "專案文件"} · {r.source_location === "REMOTE" ? "遠端" : "本機"}{r.remote_evidence_id ? ` · ${r.remote_evidence_id}` : ""}</small></p>
              {r.provenance_variants?.length > 0 && <details><summary>相同文件的其他來源</summary><pre>{JSON.stringify(r.provenance_variants, null, 2)}</pre></details>}
              {r.retrieval_trace_id && new URLSearchParams(window.location.search).get("retrievalDebug") === "1" && (
                <details>
                  <summary>Retrieval v2 除錯分數</summary>
                  <pre>{JSON.stringify({
                    trace: r.retrieval_trace_id, channels: r.retrieval_channels,
                    lexical: r.lexical_score, semantic: r.semantic_score,
                    candidate: r.candidate_score, rerank: r.rerank_score,
                    rerank_applied: r.rerank_applied, authority: r.authority_score,
                    temporal: r.temporal_score, version: r.document_version,
                    status: r.document_status, final: r.final_score,
                    fallback: r.fallback_reason,
                  }, null, 2)}</pre>
                </details>
              )}
              {r.graph_paths && (
                <details>
                  <summary>圖譜匹配路徑</summary>
                  {r.graph_paths.map((p: any, i: number) => (
                    <p key={i}>
                      {p.matched_node} · {p.hop_distance} 跳 ·{" "}
                      {p.paths.map((e: any) => e.relation).join(" → ")}
                    </p>
                  ))}
                </details>
              )}
              <h3>{r.title}</h3>
              <p>{r.text}</p>
              <small>{r.reason}</small>
              <div className="actions">
                {r.source_location !== "REMOTE" && sourceButton(r.source_id)}
                {(() => {
                  const attached = r.source_location === "REMOTE"
                    ? project.sources.some((s: any) => s.meta?.remote_key === `${r.provider_id}:${r.remote_evidence_id}`)
                    : project.sources.some((s: any) => s.id === r.source_id);
                  return <button
                    className={attached ? "" : "primary"}
                    disabled={busy || attached}
                    onClick={() =>
                      act(async () => {
                        const result = r.source_location === "REMOTE"
                          ? await api("/projects/" + pid + "/knowledge/attach-remote", "POST", {
                            provider_id: r.provider_id, remote_evidence_id: r.remote_evidence_id, query: r.matched_query || query,
                          })
                          : await api("/projects/" + pid + "/knowledge/attach", "POST", { source_id: r.source_id });
                        setNotice(`${result.message}。重新分析後會納入需求比對。`);
                      })
                    }
                  >
                    {attached ? "✓ 已是專案依據" : "加入專案依據"}
                  </button>;
                })()}
              </div>
            </article>
          ))}
        </>
      );
    if (which === 3) return sourcesView;
    if (which === 4)
      return (
        <>
          <h2>每一場會議，都保留脈絡</h2>
          <p>日期代表實際討論時間。較新的會議不會自動凌駕正式規範。</p>
          <div className="card">
            <h3>新增會議</h3>
            <div className="form-grid">
              <label>
                會議標題
                <input
                  value={meetingForm.title}
                  onChange={(e) =>
                    setMeetingForm({ ...meetingForm, title: e.target.value })
                  }
                />
              </label>
              <label>
                實際討論日期
                <input
                  type="date"
                  value={meetingForm.meeting_date}
                  onChange={(e) =>
                    setMeetingForm({
                      ...meetingForm,
                      meeting_date: e.target.value,
                    })
                  }
                />
              </label>
              <label>
                開始時間（臺北時間；同日多場必填）
                <input
                  type="time"
                  value={meetingForm.started_at}
                  onChange={(e) =>
                    setMeetingForm({
                      ...meetingForm,
                      started_at: e.target.value,
                    })
                  }
                />
              </label>
              <label>
                參與者（逗號分隔）
                <input
                  value={meetingForm.participants}
                  onChange={(e) =>
                    setMeetingForm({
                      ...meetingForm,
                      participants: e.target.value,
                    })
                  }
                />
              </label>
            </div>
            <label>
              逐字稿
              <textarea
                rows={5}
                value={meetingForm.transcript}
                onChange={(e) =>
                  setMeetingForm({ ...meetingForm, transcript: e.target.value })
                }
              />
            </label>
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  await api("/projects/" + pid + "/meetings", "POST", {
                    ...meetingForm,
                    participants: meetingForm.participants
                      .split(/[,，]/)
                      .map((s) => s.trim())
                      .filter(Boolean),
                  });
                  setMeetingForm({
                    title: "",
                    meeting_date: "",
                    started_at: "",
                    participants: "",
                    transcript: "",
                  });
                })
              }
            >
              保存會議
            </button>
            <button
              disabled={busy}
              onClick={() =>
                act(() => api("/projects/" + pid + "/wait-meeting", "POST"))
              }
            >
              保存狀態並等待會議
            </button>
          </div>
          {project.meetings.map((m: any) => (
            <MeetingCard key={m.id} meeting={m} busy={busy} act={act} />
          ))}
        </>
      );
    if (which === 5)
      return (
        <>
          <div className="section-head">
            <div>
              <h2>AI 需求變更分析報告</h2>
              <p>先看現況、目標與建議變更；來源比對與衝突細節可在下方展開。</p>
            </div>
            <button
              className="primary"
              disabled={busy || project.runtime === "RUNNING"}
              onClick={() =>
                act(() => api(`/projects/${pid}/analysis/run`, "POST"))
              }
            >
              執行／更新分析
            </button>
          </div>
          {project.state.analysis_stale && (
            <div className="banner">
              證據已更新，請重新分析。已定版文件仍保持不變。
            </div>
          )}
          {project.state.change_analysis && <section className="card change-report" aria-label="需求變更分析報告">
            <h3>需求目標</h3>
            <p>{project.state.change_analysis.goal || project.description || "待補充需求目標"}</p>
            <p className="change-report-summary">{project.state.change_analysis.summary}</p>
            {project.state.change_analysis.model_status === "PENDING" && <p className="feedback loading" role="status">AI 正在撰寫正式需求與驗收建議，請稍候…</p>}
            {project.state.change_analysis.model_status === "ERROR" && <p className="feedback warning">模型提案未完成，已保留有證據的基礎分析：{project.state.change_analysis.model_error}</p>}
            <div className="change-report-grid">
              {project.state.change_analysis.proposals.filter((item: any) => item.needs_review ?? true).map((item: any) => <details className="change-proposal" key={item.requirement_id}>
                <summary><span className={`badge change-${item.status.toLowerCase()}`}>{label(item.status)}</span> {item.name}</summary>
                <p><strong>Before：</strong>{item.current_state}</p>
                <p><strong>After：</strong>{item.target_state}</p>
                <p><strong>差異：</strong>{item.gap_delta}</p>
                <p><strong>建議正式需求：</strong>{item.proposed_requirement.statement}</p>
                <p><strong>技術影響：</strong>{item.impact_analysis.technical.map((impact: any) => impact.description).join("；")}</p>
                <p><strong>驗收：</strong>{item.proposed_requirement.acceptance_criteria.join("；")}</p>
                {item.model_recommendation_reason && <p><strong>推薦理由：</strong>{item.model_recommendation_reason}</p>}
                {!!item.alternatives?.length && <div><strong>可行選項：</strong>{item.alternatives.map((option: any, index: number) => <p key={index}>
                  {typeof option === "string" ? option : <><strong>{option.name || option.title || `方案 ${index + 1}`}</strong>
                    {option.benefit && ` · 優點：${option.benefit}`}{option.tradeoff && ` · 取捨：${option.tradeoff}`}
                    {option.recommended && " · AI 推薦"}</>}
                </p>)}</div>}
                <p><strong>證據支持：</strong>{item.evidence_uncertainty.support === "DIRECT" ? "直接支持" : "資訊不足"}</p>
                {item.evidence_uncertainty.open_questions.map((issue: string, i: number) => <p className="execution-warning" key={i}>待確認：{issue}</p>)}
                <details><summary>來源與引用段落</summary>{item.evidence_uncertainty.references.map((ref: any, i: number) => <div className="evidence" key={i}>
                  <strong>{ref.source_title}</strong><p>{ref.statement}</p>{sourceButton(ref.source_id)}
                </div>)}</details>
              </details>)}
            </div>
            {project.state.change_analysis.proposals.some((item: any) => item.needs_review === false) && <details>
              <summary>其餘 {project.state.change_analysis.proposals.filter((item: any) => item.needs_review === false).length} 項無需處理（現行規範、已符合或重複）</summary>
              {project.state.change_analysis.proposals.filter((item: any) => item.needs_review === false).map((item: any) => <p key={item.requirement_id}><span className="badge">{label(item.status)}</span> {item.name}：{item.duplicate_of ? "與其他提案重複，已合併" : item.target_state || item.current_state}</p>)}
            </details>}
            {!!project.state.change_analysis.critical_questions.length && <p className="execution-warning">有 {project.state.change_analysis.critical_questions.length} 項須明確決策，請到「提案審閱」處理。</p>}
            <button className="primary" onClick={() => setPage(6)}>審閱整體提案</button>
          </section>}
          <details className="card"><summary>詳細資訊：原始來源比對矩陣與證據</summary>
          {!!project.state.source_qualification?.length && (
            <details className="card">
              <summary>本案來源相關性與證據角色</summary>
              {project.state.source_qualification.map((s: any) => (
                <div className="evidence" key={s.source_id}>
                  <strong>{s.title}</strong>
                  <p>
                    {label(s.evidence_role)} · 相關度{" "}
                    {Math.round(s.case_relevance_score * 100)}%
                  </p>
                  <p>{s.case_relevance_reason}</p>
                  {sourceButton(s.source_id)}
                </div>
              ))}
            </details>
          )}
          {project.requirements.length === 0 ? (
            <div className="empty">先加入來源，再執行分析。</div>
          ) : (
            project.requirements.map((r: any) => (
              <article className="card" key={r.id}>
                <span className="badge">{label(r.status)}</span>
                <h3>{r.topic}</h3>
                <p>{r.statement}</p>
                {r.evidence.map((f: any, i: number) => (
                  <div className="evidence" key={i}>
                    <small>
                      {f.meeting_date || "文件"} · {f.validity || f.kind}
                    </small>
                    <p>{f.statement}</p>
                    {(f.subject || f.condition || f.effective_at) && <small>主體：{f.subject || r.topic} · 條件：{f.condition || "一般"} · 生效：{f.effective_at || "未載明"}</small>}
                    {f.conflict_reason && <p className="execution-warning">{f.conflict_reason}；請對照雙方原文與來源。</p>}
                    {sourceButton(f.source_id)}
                  </div>
                ))}
              </article>
            ))
          )}
          </details>
        </>
      );
    if (which === 6 && project.state.change_analysis) {
      const report = project.state.change_analysis;
      const reviewed = project.state.change_review?.status === "APPROVED" && project.state.change_review?.fingerprint === report.fingerprint;
      const toReview = report.proposals.filter((item: any) => item.needs_review ?? true);
      const others = report.proposals.filter((item: any) => !(item.needs_review ?? true));
      const names: Record<string, string> = Object.fromEntries(report.proposals.map((item: any) => [item.requirement_id, item.name]));
      const decided = (row: any) => ["ACCEPT", "MODIFY", "REJECT"].includes(row?.decision?.action) ||
        (row?.decision?.action === "PENDING" && row?.decision?.category === "OPEN_ISSUE" && row?.decision?.rationale);
      const unresolved = report.critical_questions.filter((item: any) =>
        !decided(project.requirements.find((r: any) => r.id === item.requirement_id)));
      const open = toReview.filter((item: any) => !decided(project.requirements.find((r: any) => r.id === item.requirement_id)));
      const blockedReason = project.state.analysis_stale ? "來源已變更，請先更新分析。" : project.runtime === "RUNNING" || report.model_status === "PENDING" ? "AI 工作執行中，請稍候。" : "";
      return <>
        <h2>審閱變更提案</h2>
        <p>只列出需要你判斷的項目。採納或拒絕不需要填理由；想修改時寫下你的想法，AI 會整理成正式需求。</p>
        <div className="card review-summary">
          <strong>{toReview.length} 項需審閱 · 尚未處理 {open.length} 項{unresolved.length ? ` · ${unresolved.length} 項須先決策` : ""}</strong>
          <p>{reviewed ? "✓ 整體提案已審閱，可產生 PRD 草稿。" : unresolved.length
            ? "請先處理標示「須決策」的項目，其餘可一次接受。"
            : "按下後：未逐項處理的提案依 AI 建議納入，現行規範列為背景，資訊不足者列為待決事項，並直接產生 PRD 草稿。"}</p>
          <div className="actions">
            <button className="primary" disabled={busy || !!blockedReason || !!unresolved.length || reviewed}
              onClick={() => act(async () => {
                await api(`/projects/${pid}/change-analysis/review`, "POST", {action: "APPROVE", fingerprint: report.fingerprint});
                await queueAndWait("PRD", { project_id: pid });
                setPage(8);
              }, "已接受提案並產生 PRD 草稿")}>
              接受提案並產生 PRD 草稿
            </button>
            {reviewed && <button className="primary" disabled={busy || !!unresolved.length} onClick={() => act(async () => {
              await queueAndWait("PRD", { project_id: pid }); setPage(8);
            })}>{project.state.draft_id ? "開啟 PRD 草稿" : "重新產生 PRD 草稿"}</button>}
          </div>
        </div>
        {toReview.length === 0 && <p className="feedback success">沒有需要你判斷的變更；可直接接受整體提案。</p>}
        {toReview.map((item: any) => {
          const row = project.requirements.find((r: any) => r.id === item.requirement_id);
          const question = report.critical_questions.find((q: any) => q.requirement_id === item.requirement_id);
          if (!row) return null;
          return <section key={item.requirement_id}>
            {question && <p className="execution-warning">須決策：{question.question}{question.options?.length ? `（可比較：${question.options.join("／")}）` : ""}</p>}
            <ReviewCard row={row} proposal={item} busy={busy} act={act} sourceButton={sourceButton}
              criticalReason={question?.reason} blockedReason={blockedReason} />
          </section>;
        })}
        {others.length > 0 && <details className="card"><summary>無需處理的 {others.length} 項（現行規範、已符合或重複）</summary>
          {others.map((item: any) => <p key={item.requirement_id}><span className="badge">{label(item.status)}</span> {item.name}：
            {item.paired_current_state ? `已作為「${names[item.duplicate_of] || "相關提案"}」的現況` : item.duplicate_of ? `與「${names[item.duplicate_of] || "其他提案"}」重複，已合併` : item.target_state || item.current_state}</p>)}
        </details>}
      </>;
    }
    if (which === 6)
      return (
        <>
          <h2>讓每個決策有依據</h2>
          <p>人工確認後才會納入 PRD。修改、拒絕或解決衝突時請記錄理由。</p>
          <div className="review-summary">
            <strong>
              已完成{" "}
              {
                project.requirements.filter((r: any) =>
                  ["ACCEPT", "MODIFY", "REJECT"].includes(r.decision.action),
                ).length
              }{" "}
              / {project.requirements.length} 項
            </strong>
            <span>綠色：接受 · 藍色：修改 · 紅色：拒絕 · 黃色：待確認</span>
            {project.state.analysis_stale ? (
              <p className="execution-warning">
                來源已變更，請先更新 AI 分析，才能保存新的決策。
                <button onClick={() => setPage(5)}>前往 AI 分析</button>
              </p>
            ) : (
              <p>
                {project.requirements.some(
                  (r: any) =>
                    !["ACCEPT", "MODIFY", "REJECT"].includes(r.decision.action),
                )
                  ? "尚有未完成決策，請逐項確認。"
                  : project.requirements.length
                    ? "全部決策已完成，可產生待審閱 PRD。"
                    : "尚無候選需求，請先執行 AI 分析。"}
              </p>
            )}
          </div>
          <div className="actions">
            <button
              className="primary"
              disabled={
                busy ||
                project.runtime === "RUNNING" ||
                project.state.analysis_stale ||
                !project.requirements.length ||
                project.requirements.some(
                  (r: any) =>
                    !["ACCEPT", "MODIFY", "REJECT"].includes(r.decision.action),
                )
              }
              onClick={() =>
                act(async () => {
                  await queueAndWait("PRD", { project_id: pid });
                  setPage(8);
                })
              }
            >
              產生待審閱 PRD
            </button>
            <button onClick={() => setPage(4)}>需要會議釐清</button>
          </div>
          {project.state.agent_judgement && <section className="card" aria-label="Agent 證據審閱">
            <h3>Agent 證據審閱 · {project.state.agent_judgement.status}</h3>
            {project.state.agent_judgement.error && <p className="feedback warning">{project.state.agent_judgement.error}</p>}
            {project.state.agent_judgement.items?.map((item: any) => <p key={item.requirement_id}>
              {item.requirement_id}：{item.recommendation} · {item.explanation}
            </p>)}
          </section>}
          {project.state.decision_advice && <section className="card" aria-label="Decision 建議">
            <h3>Decision 模型建議 · 不代替人工確認</h3>
            {project.state.decision_advice.error && <p className="feedback warning">{project.state.decision_advice.error}</p>}
            {project.state.decision_advice.items?.map((item: any) => <p key={item.requirement_id}>
              {item.requirement_id}：{item.error || `建議人工釐清分數 ${Number(item.human_review_score).toFixed(2)}（非正確率）`}
            </p>)}
          </section>}
          {project.requirements.map((r: any) => (
            <ReviewCard
              busy={busy}
              act={act}
              sourceButton={sourceButton}
              row={r}
              blockedReason={
                project.state.analysis_stale
                  ? "來源已變更，請先回到 AI 分析更新結果後再決策。"
                  : project.runtime === "RUNNING"
                    ? "AI 工作執行中，請等待完成或暫停後再保存決策。"
                    : ""
              }
              key={r.id}
            />
          ))}
        </>
      );
    if (which === 7)
      return (
        <>
          <h2>IT 交付</h2>
          <p>給 SA／IT 的技術設計、交付缺項與圖表。業務 PRD 可以先定版，這裡的內容另外審閱。</p>
          {revision ? <>
          {revision && <TechnicalDeliveryPanel revision={revision} sources={project?.sources || []} requirements={project?.requirements || []} isClient={isClient} act={act} onRevisionCreated={setRevisionId} />}
          {revision && <section className="card delivery-readiness" aria-label="PRD 缺項參考">
            <div className="row"><div><span className="eyebrow">OPTIONAL REFERENCE</span><h2>PRD 缺項參考</h2></div>
              <label className="checkbox"><input type="checkbox" checked={showItReference} onChange={(event) => setShowItReference(event.target.checked)} /> 顯示 IT 交付參考清單</label></div>
            <p className="muted">這份清單只協助 PM 查看需求、驗收與技術資訊缺項，不是業務 PRD 定版的門檻。正式交付 IT 前仍須由相關角色審閱。</p>
            {showItReference && <><span className="badge warn">{deliveryCheck?.readiness === "BLOCKED" ? `${deliveryCheck.blocking_count} 項參考缺項` : deliveryCheck?.readiness === "READY" ? "未發現缺項" : "檢查中"}</span>
            <p>{deliveryCheck?.readiness === "ERROR" ? deliveryCheck.message : deliveryCheck ? (deliveryCheck.blocking_count ? "以下項目可作為補件參考。" : "目前未發現參考缺項。") : "正在核對需求、驗收、技術模型與圖表來源…"}</p>
            {deliveryCheck?.blocking_gaps?.length > 0 && <div className="delivery-groups">
              {Object.entries(deliveryCheck.blocking_gaps.reduce((groups: Record<string, any[]>, gap: any) => {
                (groups[gap.code] ||= []).push(gap); return groups;
              }, {})).map(([code, rows]) => {
                const items = rows as any[];
                const label = ({ MISSING_ACCEPTANCE: "需求缺少驗收案例", MISSING_EVIDENCE: "需求缺少來源", MISSING_DECISION: "需求尚未確認", MISSING_IMPACT: "技術影響尚未逐項判定", IT_VALIDATION_PENDING: "技術模型驗證尚未完成" } as Record<string, string>)[code]
                  || items[0].message.replace(/^缺少已審閱的/, "").replace(/；.*/, "");
                return <details key={code} className="delivery-gap-group"><summary>{label} · {items.length} 項</summary>
                  {items.map((gap: any, index: number) => <div className="delivery-gap" key={`${gap.item_id || index}`}>
                    <span>{gap.message}</span><small>建議補件角色：{gap.owner_role}</small>
                  </div>)}
                </details>;
              })}
            </div>}</>}
          </section>}
            {revision.content.diagrams.map((d: any, i: number) => (
              <DiagramEditor
                revision={revision}
                act={act}
                key={revision.id + i + d.mermaid}
                diagram={d}
                index={i}
              />
            ))}
          </> : (
            <div className="empty"><p>先產生 PRD 草稿，才會有技術設計與圖表。</p></div>
          )}
        </>
      );
    if (which === 8)
      return (
        <>
          <div className="section-head">
            <div>
              <h2>PRD 預覽</h2>
              {revision && (
                <div className="actions">
                  <button onClick={() => act(async () => {
                    setNotice("正在產生 PRD 候選建議，請稍候…");
                    setPrdAdvice({ ...(await api(`/prd-revisions/${revision.id}/assist/prd`, "POST", undefined, 200000)), revision_id: revision.id });
                    setNotice("PRD 建議已產生，請核對來源及待確認項目。");
                  })}>AI 檢查 PRD 與驗收缺漏</button>
                  <button
                    onClick={() =>
                      act(async () =>
                        setCustomProjection({
                          id: revision.id,
                          ...(await api(
                            `/prd-revisions/${revision.id}/${revision.status === "IN_REVIEW" ? "presentation" : "projection"}`,
                            revision.status === "IN_REVIEW" ? "PUT" : "GET",
                          )),
                        }),
                      )
                    }
                  >
                    套用目前設定的模板
                  </button>
                  <a href={`/api/prd-revisions/${revision.id}/export-template`}>
                    另存目前模板 Word
                  </a>
                </div>
              )}
              <p>回答下方問題補齊空白章節，或直接編修需求；確認後建立不可覆寫的業務基準版本。</p>
            </div>
            {revision?.status === "IN_REVIEW" && (
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  act(
                    () =>
                      api(
                        "/prd-revisions/" + revision.id + "/baseline",
                        "POST",
                      ),
                    "正式版本已建立",
                  )
                }
              >
                確認並建立業務 Baseline
              </button>
            )}
          </div>
          {revision && revision.id === project.state.draft_id && <GapPanel revision={revision} onChange={() => void refresh()} />}
          {prdAdvice?.result && revision && prdAdvice.revision_id === revision.id && <section className="card">
            <h3>AI 建議 · 採用後才會寫入草稿</h3>
            {prdAdvice.result.candidates?.map((item: any, i: number) => <div className="prd-row" key={i}>
              <h4>{item.title}</h4><p>{item.suggestion}</p>
              {revision.status === "IN_REVIEW" && <button disabled={busy || item.applied} onClick={() => act(async () => {
                await api(`/prd-revisions/${revision.id}/advice/apply`, "POST", { kind: item.kind, text: item.suggestion });
                item.applied = true;
                setNotice(`已採用：${item.title}`);
              })}>{item.applied ? "✓ 已採用" : "採用到 PRD"}</button>}
              <small>來源：{item.source_refs?.length
                ? item.source_refs.map((id: string) => <span key={id}>{sourceButton(id)} </span>)
                : "尚缺"} · 待確認：{item.uncertainty || "無"}</small>
            </div>)}
            {prdAdvice.result.missing_information?.map((item: string, i: number) => <p key={i}>待補：{item}</p>)}
          </section>}
          {revision ? (
            <article className="paper">
              <span className="eyebrow">PRODUCT REQUIREMENTS DOCUMENT</span>
              <h1>{revision.content.title}</h1>
              <p>
                {revision.version} · {label(revision.status)}
              </p>
              <hr />
              {(() => {
                const projection =
                  customProjection?.id === revision.id
                    ? customProjection
                    : revision.projection;
                return (
                  <>
                    <p className="muted">
                      模板：{projection?.template_name} ·
                      定版下載保留原始模板；另存投影不修改歷史版本。
                    </p>
                    {projection?.warnings?.map((w: string) => (
                      <p className="feedback warning" key={w}>
                        {w}
                      </p>
                    ))}
                    {projection?.sections?.map(
                      (section: any, index: number) => (
                        <section className="prd-section" key={section.key}>
                          <h2>
                            {index + 1}. {section.title}
                          </h2>
                          {section.paragraphs.map((text: string, i: number) => (
                            <p className="prewrap" key={i}>
                              {text}
                            </p>
                          ))}
                          {section.rows.map((row: any, i: number) => (
                            <div className="prd-row" key={row.id + String(i)}>
                              <small>{row.id}</small>
                              <h3>{row.title}</h3>
                              <p>{row.description}</p>
                              {row.detail && <small>{row.detail}</small>}
                              {section.key === "requirements" && revision.id === project.state.draft_id &&
                                <RequirementRowEditor revision={revision} row={row} onChange={() => void refresh()} />}
                              {row.source_refs?.length > 0 && (
                                <details>
                                  <summary>
                                    來源追溯（{row.source_refs.length}）
                                  </summary>
                                  {row.source_refs.map((id: string) => (
                                    <span key={id}>{sourceButton(id)} </span>
                                  ))}
                                </details>
                              )}
                            </div>
                          ))}
                          {section.diagrams.map((d: any, i: number) => (
                            <div key={i}>
                              <h3>{d.title}</h3>
                              <p>{d.warning}</p>
                              <Diagram source={d.mermaid} />
                            </div>
                          ))}
                        </section>
                      ),
                    )}
                  </>
                );
              })()}
            </article>
          ) : (
            <div className="empty">
              <p>還沒有 PRD 草稿。{project.state.change_analysis ? "審閱變更提案後即可產生。" : "先完成需求分析，再審閱提案。"}</p>
              <div className="actions">
                {project.state.change_analysis
                  ? <button className="primary" onClick={() => setPage(6)}>前往提案審閱</button>
                  : <button className="primary" onClick={() => setPage(5)}>前往變更分析</button>}
              </div>
            </div>
          )}
        </>
      );
    return (
      <>
        <h2>PRD 的每一個版本，都留得住</h2>
        <p>新資訊會形成變更請求。修訂只更新受影響內容，歷史版本可獨立下載。</p>
        {current && (
          <div className="baseline">
            <span>目前正式基準</span>
            <h2>{current.version}</h2>
            <a
              className="button primary"
              href={"/api/prd-revisions/" + current.id + "/download"}
            >
              下載 PRD.docx ↓
            </a>
          </div>
        )}
        <h3>變更請求</h3>
        {project.changes.length ? (
          project.changes.map((c: any) => (
            <ChangeCard
              busy={busy}
              act={act}
              change={c}
              key={c.id + c.status}
            />
          ))
        ) : (
          <p className="muted">尚無變更請求；加入新證據即可開始修訂。</p>
        )}
        <div className="actions">
          <button onClick={() => setPage(3)}>加入新證據</button>
          <button disabled={busy} onClick={() => act(continueRun)}>
            分析／繼續修訂
          </button>
          <button
            disabled={busy}
            onClick={() =>
              act(async () => {
                await queueAndWait("PRD", { project_id: pid });
                setPage(8);
              })
            }
          >
            產生修訂草稿
          </button>
        </div>
        <h3>版本時間軸</h3>
        {project.revisions.map((r: any) => (
          <article className="card" key={r.id}>
            <div className="row">
              <h3>{r.version}</h3>
              <span className="badge">{label(r.status)}</span>
            </div>
            <small>
              {new Date(r.created_at).toLocaleString("zh-TW")} ·{" "}
              {r.parent_revision_id ? "承接前版" : "初始版本"}
            </small>
            <div className="actions">
              <button
                onClick={() => {
                  setRevisionId(r.id);
                  setPage(8);
                }}
              >
                預覽此版
              </button>
              {["BASELINED", "SUPERSEDED"].includes(r.status) && (
                <a
                  className="button"
                  href={"/api/prd-revisions/" + r.id + "/download"}
                >
                  下載此版
                </a>
              )}
            </div>
            <details>
              <summary>檢視差異 · {r.diff.length} 項</summary>
              {r.diff.map((d: any, i: number) => (
                <div className="diff" key={i}>
                  <del>{d.before?.statement || "（新增需求）"}</del>
                  <ins>{d.after?.statement || "（移除需求）"}</ins>
                  {d.source_refs.map((s: string) => (
                    <span key={s}>{sourceButton(s)}</span>
                  ))}
                </div>
              ))}
            </details>
          </article>
        ))}
      </>
    );
  }
  function renderPage() {
    if ([1, 2, 3].includes(page))
      return <>{renderSingle(1)}{renderSingle(3)}{renderSingle(2)}</>;
    if ([5, 6].includes(page)) {
      if (!project.state.change_analysis) return renderSingle(5);
      return <>
        {renderSingle(6)}
        <details className="card analysis-details">
          <summary>分析細節：來源比對與證據（需要時再看）</summary>
          {renderSingle(5)}
        </details>
      </>;
    }
    return renderSingle(page);
  }


  return (
    <div className={`app${settingsOpen ? " settings-active" : ""}${leftCollapsed ? " left-collapsed" : ""}${rightCollapsed ? " right-collapsed" : ""}${mobileLeftOpen ? " mobile-left-open" : ""}${mobileRightOpen ? " mobile-right-open" : ""}`} onClickCapture={clickFeedback}>
      {(mobileLeftOpen || mobileRightOpen) && <button className="side-backdrop" aria-label="關閉側欄" onClick={() => { setMobileLeftOpen(false); setMobileRightOpen(false); }} />}
      <aside id="project-sidebar" className="sidebar" aria-label="專案側欄">
        <button className="side-toggle left-toggle" onClick={toggleLeft} aria-label={leftCollapsed && !mobileLeftOpen ? "展開專案側欄" : "收合專案側欄"} aria-expanded={!leftCollapsed || mobileLeftOpen} aria-controls="project-sidebar">{leftCollapsed && !mobileLeftOpen ? "☰" : "◀"}</button>
        {leftCollapsed && !mobileLeftOpen ? <span className="brand-mark rail-brand">彤</span> : <>
        <div className="brand">
          <span className="brand-mark">彤</span>
          <div>
            <strong>彤靈談心</strong>
            <small>REQUIREMENT INTELLIGENCE</small>
          </div>
        </div>
        <nav className="sidebar-home" aria-label="主要導覽">
          <button className={!settingsOpen && homeOpen ? "selected" : ""} onClick={() => { setHomeOpen(true); setSettingsOpen(false); setMobileLeftOpen(false); }}>⌂ 專案總覽</button>
        </nav>
        <div className="sidebar-project-heading"><span>最近專案</span><div><small>{projects.length} 個專案</small><button onClick={() => { setCreateOpen(true); setMobileLeftOpen(false); }} aria-label="建立新專案" title="建立新專案">＋</button></div></div>
        <nav className="sidebar-projects" aria-label="最近專案">
          {projects.slice(0, 5).map((p) => (
            <button
              className={!settingsOpen && !homeOpen && pid === p.id ? "selected" : ""}
              key={p.id}
              onClick={() => { setPid(p.id); setHomeOpen(false); setSettingsOpen(false); setMobileLeftOpen(false); }}
            >
              <span>◈</span>
              <span className="nav-project-title">{p.name}</span>
            </button>
          ))}
          {pid && !projects.slice(0, 5).some((p) => p.id === pid) && projects.filter((p) => p.id === pid).map((p) => (
            <button className={!settingsOpen && !homeOpen ? "selected" : ""} key={p.id} onClick={() => { setPid(p.id); setHomeOpen(false); setSettingsOpen(false); setMobileLeftOpen(false); }}>
              <span>◈</span><span className="nav-project-title">{p.name}</span>
            </button>
          ))}
        </nav>
        {projects.length > 5 && <button className="sidebar-all-projects" onClick={() => { setHomeOpen(true); setSettingsOpen(false); setMobileLeftOpen(false); }}>查看全部專案 →</button>}
        {isClient && trashed.length > 0 && <details className="client-trash">
          <summary>最近刪除 · {trashed.length}</summary>
          {trashed.map((p) => <div className="client-trash-item" key={p.id}><span>{p.name}</span><button disabled={busy} onClick={() => act(async () => {
            await api(`/projects/${p.id}/restore`, "POST");
            await projectsChanged();
          }, "專案已復原")}>復原</button></div>)}
        </details>}
        <div className="sidebar-bottom">
          <div className="local-dot">{isClient ? "個人專案 · 主機共用知識" : "本機優先 · 證據可追溯"}</div>
          <button className={settingsOpen && settingsTab === "appearance" ? "selected" : ""} onClick={() => { setSettingsTab("appearance"); setSettingsOpen(true); setMobileLeftOpen(false); }}>◐ 外觀與主題</button>
          {!isClient && <button className={settingsOpen && settingsTab !== "appearance" ? "selected" : ""} onClick={() => { setSettingsTab("model"); setSettingsOpen(true); setMobileLeftOpen(false); }}>⚙ 主機設定</button>}
          {isClient && <button onClick={() => act(async () => { await api("/logout", "POST"); location.reload(); })}>離開工作台</button>}
        </div>
        </>}
      </aside>
      <main aria-hidden={settingsOpen || mobileLeftOpen || mobileRightOpen}>
        <header>
          <button className="side-toggle header-left-toggle" onClick={toggleLeft} aria-label={mobileLeftOpen ? "收合專案側欄" : "展開專案側欄"} aria-expanded={mobileLeftOpen} aria-controls="project-sidebar">☰</button>
          {(homeOpen || settingsOpen) && <div className="mobile-brand">{settingsOpen ? settingsTab === "appearance" ? "外觀與主題" : "主機設定" : "專案總覽"}</div>}
          {!homeOpen && !settingsOpen && <select
            className="mobile-project"
            aria-label="切換專案"
            value={pid}
            onChange={(e) => { setPid(e.target.value); setHomeOpen(false); }}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>}
          <div className="breadcrumb">
            工作空間 <span>/</span> {settingsOpen ? settingsTab === "appearance" ? "外觀與主題" : "主機設定" : homeOpen ? "專案總覽" : project?.name || "開始新專案"}
          </div>
          <div className="header-actions">
            <button className="side-toggle header-right-toggle" onClick={toggleRight} aria-label={mobileRightOpen ? "收合工作動態" : workActive ? "展開工作動態，工作執行中" : "展開工作動態"} aria-expanded={mobileRightOpen} aria-controls="work-sidebar">{workActive ? "◉" : "◫"}</button>
            {project && !settingsOpen && !homeOpen && !manual && (
              <button className="mode" onClick={() => detailView ? setDetailView(false) : openStep(project.state.step || 1)}>
                {detailView ? "回到 Agent 對話" : "查看詳細步驟"}
              </button>
            )}
            {project && !settingsOpen && !homeOpen && (
              <button className="mode subtle-mode" title={manual ? "由 Agent 自動檢索、分析並在需要時請你確認" : "停用自動推進，由你逐步操作"} onClick={() => act(switchMode)}>
                {manual ? "改用 Agent 引導" : "改為手動流程"}
              </button>
            )}
            <button
              className="mobile-create"
              onClick={() => setCreateOpen(true)}
            >
              ＋
            </button>
            {isClient && project && <button disabled={busy} onClick={() => {
              if (!window.confirm(`將「${project.name}」移至最近刪除？之後仍可復原。`)) return;
              void act(async () => {
                await api(`/projects/${pid}`, "DELETE");
                await projectsChanged(pid);
              }, "專案已移至最近刪除", true);
            }}>刪除專案</button>}
          </div>
        </header>
        {error && (
          <div role="alert" className="error global-error">
            {error}
            <button onClick={() => setError("")}>關閉</button>
          </div>
        )}
        {notice && (
          <div role="status" className="toast">
            {notice}
          </div>
        )}
        {homeOpen ? <ProjectHub isClient={isClient} onCreate={() => setCreateOpen(true)} onDemo={isClient ? undefined : applyDemo}
          onOpen={(id, step) => { setPid(id); if (step) setPage(step); setHomeOpen(false); setMobileLeftOpen(false); }} /> : !project ? (
          <div className="welcome">
            <span className="eyebrow">TONG LING · PERSONAL WORKSPACE</span>
            <h1>
              把散落的需求，
              <br />
              談成可信的版本。
            </h1>
            <p>從需求與證據出發，整理分歧、確認決策，持續維護你的 PRD。</p>
            <button className="primary" onClick={() => setCreateOpen(true)}>
              建立第一個專案 →
            </button>
            {!isClient && <button className="demo-launch" disabled={busy} onClick={applyDemo}>
              ✦ 先用範例體驗
            </button>}
          </div>
        ) : showSteps ? (
          <>
            {nextStep && <div className="next-step-bar" role="status">
              <span className="eyebrow">下一步</span><span>{nextStep.text}</span>
              {nextStep.action === "analyze" && project.runtime !== "RUNNING"
                ? <button className="primary" disabled={busy} onClick={() => act(() => api(`/projects/${pid}/analysis/run`, "POST"), "已開始分析")}>執行分析</button>
                : nextStep.step > 0 && placeName(page) !== placeName(nextStep.step) && <button className="primary" onClick={() => setPage(nextStep.step)}>前往「{placeName(nextStep.step)}」</button>}
            </div>}
            <div className="steps">
              {STAGES.map((stage, i) => (
                <button
                  className={stage.pages.includes(page) ? "active" : ""}
                  key={stage.label}
                  onClick={() => setPage(stage.pages[0])}
                >
                  <span>{i + 1}</span>
                  {stage.label}
                </button>
              ))}
              <span className="steps-divider" aria-hidden="true" />
              {EXTRAS.map((extra) => (
                <button
                  className={`step-extra ${page === extra.page ? "active" : ""}`}
                  key={extra.label}
                  onClick={() => setPage(extra.page)}
                >
                  {extra.label}
                </button>
              ))}
            </div>
            <div className="workspace">
              {project.state.demo_key && (
                <div className="demo-banner">
                  <span>✦ 範例專案 · 虛構測試資料</span>
                  <p>
                    已從專用資料夾讀取需求、政策、SA
                    與三場會議。先分析並確認會議，再審閱需求；所有結果仍經由正常流程產生。
                  </p>
                  <div className="actions">
                    <button onClick={() => setPage(3)}>查看來源文件</button>
                    <button onClick={() => setPage(4)}>處理會議紀錄</button>
                    <button disabled={busy} onClick={() => act(continueRun)}>
                      開始需求分析
                    </button>
                    {project.current_revision_id && (
                      <button
                        onClick={() =>
                          act(async () => {
                            await api(
                              `/projects/${pid}/demo/revision-meeting`,
                              "POST",
                            );
                            setPage(4);
                          }, "已加入正式版本後的變更會議，請分析並確認。")
                        }
                      >
                        加入 v1.1 變更範例
                      </button>
                    )}
                  </div>
                </div>
              )}
              {renderPage()}
            </div>
          </>
        ) : (
          <div className="agent">
            <div className="agent-top">
              <span className="eyebrow">YOUR REQUIREMENT COPILOT</span>
              <h1>讓需求，一步步清晰。</h1>
              <p>彤靈整理證據，你掌握重要決策。</p>
            </div>
            <div className="agent-status">
              {project.runtime === "RUNNING" && <span className="pulse" />}
              <strong>{label(project.runtime)}</strong>
              <span className="muted">
                {project.state.step
                  ? `目前階段 · ${placeName(project.state.step)}`
                  : "等待需求"}
              </span>
              <button
                onClick={() =>
                  act(() => api("/projects/" + pid + "/agent/pause", "POST"))
                }
                disabled={busy || project.runtime !== "RUNNING"}
              >
                暫停
              </button>
            </div>
            <div className="progress">
              <div style={{ width: (project.state.progress || 0) + "%" }} />
            </div>
            <div className="task-chips">
              {STAGES.map((stage, i) => (
                <button
                  type="button"
                  className={`task-chip ${STAGES.findIndex((x) => x.pages.includes(project.state.step || 1)) >= i ? "done" : ""}`}
                  key={stage.label}
                  onClick={() => openStep(stage.pages[0])}
                >
                  {stage.label}
                </button>
              ))}
            </div>
            {config?.provider === "mock" && (
              <div className="banner subtle">
                離線驗證模式：使用保守的規則抽取。若要語意分析，請在設定連接
                Ollama 或相容 API。
              </div>
            )}
            <div className="thread">
              <article className="assistant-message">
                <span className="avatar">彤</span>
                <div>
                  <h3>
                    {project.current_revision_id
                      ? "你的 PRD 可以持續演進"
                      : "從需求起點，建立共同理解"}
                  </h3>
                  <p>
                    {project.current_revision_id
                      ? "加入新會議或證據，我會整理變更影響，再由你決定是否修訂。"
                      : project.runtime === "RUNNING"
                        ? "需求起點已保存。我正在檢索可用來源並分析；遇到缺乏證據或需要決策的項目會停下來請你確認。"
                        : project.state.change_analysis
                          ? "我已比對需求與依據，整理成變更提案。下面是我查到的依據與你接下來要做的事。"
                        : project.sources.length > 0
                          ? "需求起點已保存。按「繼續處理」，彤靈會先查找可用來源，再整理待確認事項；你無須先準備附件。"
                          : "先描述需求起點，彤靈會查找可用來源；文件與會議可在需要時補充。"}
                  </p>
                </div>
              </article>
              <button
                className="text-button activity-toggle"
                onClick={() => setShowActivity(!showActivity)}
              >
                {showActivity ? "收合" : "展開"}活動紀錄 ·{" "}
                {pmEvents.length}
              </button>
              {(showActivity ? pmEvents : pmEvents.slice(-3)).map(
                (e: any) => (
                  <div className="activity" key={e.id}>
                    <span className="event-dot" />
                    <div>
                      <p>{e.message}</p>
                      <small>
                        {new Date(e.created_at).toLocaleTimeString("zh-TW", {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                        {EVENT_LABELS[e.kind] ? ` · ${EVENT_LABELS[e.kind]}` : ""}
                      </small>
                    </div>
                  </div>
                ),
              )}
              {project.state.retrieval_queries && (
                <article className="assistant-message">
                  <span className="avatar">彤</span>
                  <div>
                    <h3>我先查了共用知識</h3>
                    <p>搜尋詞：{project.state.retrieval_queries.join("、")}。找到 {project.state.retrieval_results?.length || 0} 筆，
                      已把相關的 {project.sources.filter((src: any) => src.kind !== "REQUIREMENT_TEXT").length} 份加入本案依據；不相關的不會用來推論需求。</p>
                    <div className="actions">
                      <button onClick={() => openStep(3)}>查看依據</button>
                      <button onClick={() => openStep(2)}>自己再搜尋</button>
                    </div>
                    {excludedHits.length > 0 && <details>
                      <summary>沒有採用的 {excludedHits.length} 份與原因</summary>
                      {excludedHits.map((hit: any) => <div className="evidence" key={hit.source_id}>
                        <strong>{hit.title}</strong>
                        <p className="muted">{label(hit.evidence_role)} · 相關度 {Math.round((hit.case_relevance_score || 0) * 100)}%{hit.case_relevance_reason ? ` · ${hit.case_relevance_reason}` : ""}</p>
                        <button disabled={busy || project.runtime === "RUNNING"} onClick={() => act(async () => {
                          const result = hit.source_location === "REMOTE"
                            ? await api(`/projects/${pid}/knowledge/attach-remote`, "POST", { provider_id: hit.provider_id, remote_evidence_id: hit.remote_evidence_id, query: hit.matched_query || "" })
                            : await api(`/projects/${pid}/knowledge/attach`, "POST", { source_id: hit.source_id });
                          setNotice(`${result.message}。重新分析後會納入需求比對。`);
                        })}>仍要加入</button>
                      </div>)}
                    </details>}
                  </div>
                </article>
              )}
              {nextStep && project.runtime !== "RUNNING" && project.interruptions.length === 0 && (
                <div className="attention">
                  <span className="badge">下一步</span>
                  <p>{nextStep.text}</p>
                  {nextStep.action === "analyze"
                    ? <button className="primary" disabled={busy} onClick={() => act(continueRun)}>開始分析 →</button>
                    : nextStep.step > 1 && <button className="primary" onClick={() => openStep(nextStep.step)}>前往「{placeName(nextStep.step)}」→</button>}
                </div>
              )}
              {project.interruptions.length > 0 && (
                <div className="attention">
                  <span className="badge warn">等待你確認</span>
                  <h3>{project.interruptions[0].title}</h3>
                  <p>{project.interruptions[0].summary}</p>
                  <button
                    className="primary"
                    onClick={() => setInterrupt(project.interruptions[0])}
                  >
                    查看待辦決策 →
                  </button>
                </div>
              )}
              {current && (
                <div className="baseline">
                  <span>已保存正式版本 · 專案持續開放</span>
                  <h2>PRD {current.version}</h2>
                  <a
                    className="button primary"
                    href={"/api/prd-revisions/" + current.id + "/download"}
                  >
                    下載 Word 文件 ↓
                  </a>
                </div>
              )}
            </div>
            <div className="composer-wrap">
              <div className="composer">
                <button
                  className="attach"
                  aria-label="上傳附件"
                  onClick={() => uploadRef.current?.click()}
                >
                  ＋
                </button>
                <textarea
                  aria-label="補充需求"
                  value={composer}
                  onChange={(e) => setComposer(e.target.value)}
                  placeholder={
                    project.current_revision_id
                      ? "加入新資訊，開始下一次修訂…"
                      : "描述需求、補充背景，或加入附件…"
                  }
                />
                <button
                  className="send"
                  aria-label="保存補充"
                  disabled={!composer.trim() || busy}
                  onClick={() =>
                    act(async () => {
                      await api(
                        "/projects/" + pid + "/requirement-text",
                        "POST",
                        { text: composer },
                      );
                      setComposer("");
                    })
                  }
                >
                  ↑
                </button>
              </div>
              <div className="composer-foot">
                <small />
                <button
                  className="primary"
                  disabled={busy || project.runtime === "RUNNING"}
                  onClick={() => act(continueRun)}
                >
                  {project.runtime === "ERROR" ? "重試並繼續" : "繼續處理 →"}
                </button>
              </div>
            </div>
          </div>
        )}
        <input
          hidden
          ref={uploadRef}
          type="file"
          accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md,.vtt"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) act(() => upload(f), "文件已解析並保存");
            e.target.value = "";
          }}
        />
      </main>
      <aside id="work-sidebar" className="work-sidebar" aria-label="工作動態">
        <div className="work-sidebar-head">
          <div><small>LIVE WORKSPACE</small><strong>工作動態</strong></div>
          <button className="side-toggle" onClick={toggleRight} aria-label={rightCollapsed && !mobileRightOpen ? "展開工作動態" : "收合工作動態"} aria-expanded={!rightCollapsed || mobileRightOpen} aria-controls="work-sidebar">{rightCollapsed && !mobileRightOpen ? "◫" : "▶"}</button>
        </div>
        {rightCollapsed && !mobileRightOpen ? <span className={`status-orb ${workActive ? "is-running" : !homeOpen && project?.runtime === "ERROR" ? "is-error" : ""}`} title={homeOpen ? "跨專案工作動態" : workActive ? "工作執行中" : project ? `Agent：${label(project.runtime)}` : "尚未選擇專案"} /> : <div className="work-sidebar-scroll">
          <div className="work-sidebar-intro"><span className={`status-orb ${workActive ? "is-running" : !homeOpen && project?.runtime === "ERROR" ? "is-error" : ""}`} />{homeOpen ? "跨專案工作動態 · 各案進度請看首頁" : project ? `${project.name} · ${label(project.runtime)}` : "選擇專案後顯示工作進度"}</div>
        {busy && (
          <div className="execution-card">
            <strong>正在提交或處理操作</strong>
            <p>
              <span className="spinner" />
              請求處理中，結果會顯示於下方或目前對話框。
            </p>
          </div>
        )}
        {jobs.filter((job) => noticeVisible(job.status === "COMPLETED" ? "SUCCEEDED" : job.status, job.finished_at, job.dismissed_at, Date.now()))
          .slice(0, 5).map((job) => <section className="execution-card" data-state={job.status} key={job.job_id} aria-label="背景工作狀態">
          {!["QUEUED", "RUNNING"].includes(job.status) && <button className="text-button notice-dismiss" aria-label="清除此工作通知" onClick={() => void dismissJob(job.job_id)}>知道了 ✕</button>}
          <strong>{({ AGENT: "Agent 流程", GUIDANCE: "AI 需求建議", RAG: "知識檢索", PRD: "PRD 生成" } as Record<string, string>)[job.kind] || job.kind} · {({ QUEUED: "排隊中", RUNNING: "執行中", COMPLETED: "已完成", FAILED: "失敗", CANCELLED: "已取消" } as Record<string, string>)[job.status]}</strong>
          {job.status === "QUEUED" && <p>已收到工作，正在等待可用資源。可離開頁面，稍後回來查看。</p>}
          {job.status === "RUNNING" && <p>{job.cancel_requested ? "正在取消，已完成的步驟可能需要收尾。" : `主機正在處理${job.progress ? ` · ${job.progress}%` : ""}，離開頁面不會停止。`}</p>}
          {job.status === "FAILED" && <p role="alert" className="execution-warning">{job.error || "工作失敗，可重新提交。"}</p>}
          <div className="actions">
            {["QUEUED", "RUNNING"].includes(job.status) && <button disabled={!!job.cancel_requested} onClick={() => { void api<Job>(`/jobs/${job.job_id}/cancel`, "POST").then(updateJob).catch((e) => setError(e.message)); }}>取消工作</button>}
            {["FAILED", "CANCELLED"].includes(job.status) && <button disabled={busy} onClick={() => act(async () => { updateJob(await api<Job>(`/jobs/${job.job_id}/resume`, "POST")); })}>重新提交</button>}
            {job.status === "COMPLETED" && <button onClick={() => act(async () => {
              const detail = await api<Job>(`/jobs/${job.job_id}`);
              void dismissJob(job.job_id);
              if (job.project_id) { setPid(job.project_id); setHomeOpen(false); }
              if (job.kind === "GUIDANCE") { setGuidance(detail.result); setCreateOpen(true); }
              if (job.kind === "RAG" && job.project_id) { showSearchResult(detail.result, job.project_id); setPage(2); setDetailView(true); }
              if (job.kind === "PRD") setPage(8);
              if (job.project_id) await refresh(job.project_id);
            })}>查看結果</button>}
          </div>
        </section>)}
        {project && !homeOpen && (
          <section
            className="execution-card runtime-summary"
            aria-label="Agent 狀態檢查"
          >
            <header>
              <strong>
                {manual ? "工作流程" : "Agent"}：{label(project.runtime)}
              </strong>
              <button disabled={busy} onClick={() => act(() => refresh())}>
                更新狀態
              </button>
            </header>
            <p>
              {project.runtime === "RUNNING"
                ? "正在執行，完成後才可進行下一步。詳見執行卡片。"
                : project.runtime === "WAITING_MEETING"
                  ? "Agent 已停止推進，等待你確認會議紀錄。"
                  : project.runtime === "WAITING_HUMAN"
                    ? "Agent 已完成本階段並等待人工處理，目前沒有繼續生成。"
                    : project.runtime === "ERROR"
                      ? "本次執行失敗，請查看錯誤後重試。"
                      : project.runtime === "PAUSED"
                        ? "Agent 已暫停，按繼續處理才會恢復。"
                        : "目前沒有 Agent 工作正在執行。"}
            </p>
            {project.latest_run?.error && (
              <p className="execution-warning">
                最近工作：{project.latest_run.error}
              </p>
            )}
            {syncError ? (
              <p role="alert" className="execution-warning">
                {syncError}
              </p>
            ) : (
              <small>
                最後狀態同步：
                {lastSync
                  ? new Date(lastSync).toLocaleTimeString()
                  : "讀取中"}{" "}
                · 執行中每 2 秒、閒置每 10 秒；背景頁面每 30 秒檢查
              </small>
            )}
            <div className="actions">
              {project.runtime === "RUNNING" ? (
                <button
                  disabled={busy}
                  onClick={() =>
                    act(() => api(`/projects/${pid}/agent/pause`, "POST"))
                  }
                >
                  暫停目前工作
                </button>
              ) : (
                <button disabled={busy} onClick={() => act(continueRun)}>
                  {project.runtime === "ERROR" ? "重試工作" : "繼續處理"}
                </button>
              )}
            </div>
          </section>
        )}
        <ActivityCards
          tasks={homeOpen ? globalTasks : project?.activities || []}
          onDismiss={(id) => void dismissActivity(id)}
        />
        {enrichmentJobs
          .filter((j) =>
            ["RUNNING", "QUEUED", "WAITING_RETRY", "PAUSING"].includes(
              j.status,
            ),
          )
          .map((j) => (
            <div className="execution-card" key={j.id}>
              <strong>
                {j.kind === "GRAPH_BUILD" ? "建立知識圖譜" : "生成標籤"} ·{" "}
                {j.progress_percent}%
              </strong>
              <progress max={100} value={j.progress_percent} />
              <p>
                {jobLabel(j.stage)} · {j.completed_items}/{j.total_items} · 失敗{" "}
                {j.failed_items}。工作較耗時，請耐心等候。
              </p>
            </div>
          ))}
        </div>}
      </aside>
      {createOpen && (
        <div className="overlay">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="建立專案"
          >
            <span className="eyebrow">A NEW BEGINNING</span>
            <h2>建立需求專案</h2>
            <label>
              專案名稱
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="你的產品或需求名稱"
              />
            </label>
            <label>
              需求起點
              <textarea
                rows={6}
                value={brief}
                onChange={(e) => { setBrief(e.target.value); setGuidance(null); setGuidanceError(""); }}
                disabled={guidanceBusy}
                placeholder="想解決什麼問題？有哪些已知限制？"
              />
            </label>
            <div className="guidance-panel">
              <button disabled={!brief.trim() || busy || guidanceBusy} onClick={async () => {
                setGuidanceBusy(true); setGuidanceSeconds(0); setGuidanceError(""); setGuidance(null);
                try {
                  const suggestion = await queueAndWait("GUIDANCE", { brief });
                  setGuidance(suggestion);
                  if (!name.trim()) setName(suggestion.suggested_project_name);
                } catch (cause) { setGuidanceError((cause as Error).message); }
                finally { setGuidanceBusy(false); }
              }}>✦ AI 建議需求與下一步</button>
              {guidanceBusy && <div role="status" className="feedback loading guidance-progress"><span className="spinner" /><div><strong>正在整理需求與待確認事項 · {guidanceSeconds} 秒</strong><small>模型生成可能需要數十秒；完成後可檢查建議，再決定是否採用。</small></div></div>}
              {guidanceError && <div role="alert" className="feedback warning">建議生成失敗：{guidanceError}。原始需求仍可建立專案。</div>}
              {guidance && <div role="status" className="feedback success">
                <strong>{guidance.summary}</strong>
                <p>建議釐清：{guidance.questions?.join("；")}</p>
                <button onClick={() => setBrief(guidance.refined_brief)}>套用建議需求</button>
              </div>}
              <p className="muted">建立後彤靈會自動查找依據並分析需求，需要你決定時會停下來。</p>
              <details className="create-advanced">
                <summary>進階選項</summary>
                <label>工作方式
                  <select value={initialMode} onChange={(e) => setInitialMode(e.target.value as "AGENT" | "MANUAL")}>
                    <option value="AGENT">Agent 引導（建議）</option><option value="MANUAL">手動流程</option>
                  </select>
                </label>
                {initialMode === "AGENT" && <>
                  <label className="checkbox"><input type="checkbox" checked={startOnCreate} onChange={(e) => setStartOnCreate(e.target.checked)} />建立後立即開始分析</label>
                  <label className="checkbox"><input type="checkbox" checked={delegated} onChange={(e) => setDelegated(e.target.checked)} />讓 Agent 先處理明確且無衝突的需求（衝突與定版仍由你確認）</label>
                </>}
              </details>
            </div>
            <div className="actions">
              <button
                className="primary"
                disabled={!name.trim() || !brief.trim() || busy || guidanceBusy}
                onClick={() =>
                  act(async () => {
                    const p = await api("/projects", "POST", {
                      name,
                      description: brief,
                    });
                    if (initialMode === "MANUAL") {
                      await api(`/projects/${p.id}/workflow/mode`, "POST", { mode: "MANUAL" });
                    } else {
                      await api(`/projects/${p.id}/agent/delegation`, "POST", { mode: delegated ? "AUTO_DECIDE_SAFE" : "GUIDED" });
                      if (startOnCreate) await api(`/projects/${p.id}/agent/continue`, "POST");
                    }
                    setProjects(await api("/projects"));
                    setPid(p.id);
                    setHomeOpen(false);
                    setCreateOpen(false);
                    setName("");
                    setBrief("");
                    setGuidance(null);
                    setDelegated(false);
                    setStartOnCreate(true);
                  })
                }
              >
                建立專案
              </button>
              <button onClick={() => setCreateOpen(false)}>取消</button>
            </div>
          </section>
        </div>
      )}
      {interrupt && (
        <div className="overlay">
          <section
            className="modal decision-sheet"
            role="dialog"
            aria-modal="true"
            aria-label="人工決策"
          >
            <span className="sheet-handle" />
            <span className="badge warn">需要你的參與</span>
            <h2>{interrupt.title}</h2>
            <p>{interrupt.summary}</p>
            <div className="decision-explain">
              <strong>下一步</strong>
              <p>
                {interrupt.kind === "FINAL_PRD_REVIEW_REQUIRED"
                  ? "檢查 PRD 內容、來源與圖表，確認後建立正式版本。"
                  : "開啟相關內容，對照證據並完成決策。若需要更多資料，可以先保存等待狀態。"}
              </p>
              <small>稍後處理只會關閉提醒；尚未解決的事項仍會保留。</small>
            </div>
            <div className="actions">
              <button
                className="primary"
                disabled={busy}
                onClick={() => act(() => goReview(interrupt))}
              >
                查看內容並處理 →
              </button>
              <button
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    await api(
                      "/interruptions/" + interrupt.id + "/dismiss",
                      "POST",
                    );
                    setInterrupt(null);
                  })
                }
              >
                稍後處理
              </button>
            </div>
          </section>
        </div>
      )}
      {source && (
        <div className="overlay">
          <section
            className="modal wide"
            role="dialog"
            aria-modal="true"
            aria-label="證據原文"
          >
            <div className="row">
              <h2>{source.title}</h2>
              <button onClick={() => setSource(null)}>關閉</button>
            </div>
            <p>
              {source.kind} · {source.status}
            </p>
            {source.meta?.source_url && (
              <div className="feedback warning">
                <strong>
                  外部參考 ·{" "}
                  {source.meta.coverage === "SEARCH_SNIPPET"
                    ? "僅搜尋摘要"
                    : "文章節錄"}
                </strong>
                <p>
                  {source.meta.publisher} · 發布：
                  {source.meta.published_at || "未知"}
                </p>
                <p>
                  相關度{" "}
                  {Math.round((source.meta.case_relevance_score || 0) * 100)}% ·
                  來源品質{" "}
                  {Math.round((source.meta.source_quality_score || 0) * 100)}%
                </p>
                <a
                  href={source.meta.source_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  開啟原始報導
                </a>
                <p>{source.meta.fetch_error}</p>
              </div>
            )}
            {source.chunks.map((c: any) => (
              <div className="evidence" key={c.id}>
                <small>
                  {c.meta.page ? "頁 " + c.meta.page : ""} {c.meta.heading} ·{" "}
                  {c.id.slice(0, 8)}
                </small>
                <p className="prewrap">{c.text}</p>
              </div>
            ))}
          </section>
        </div>
      )}
      {settingsOpen && config && (
        <div className="settings-page" role="main" aria-label={settingsTab === "appearance" ? "外觀與主題設定" : "主機設定"}>
          <div className="settings-page-header">
            {settingsTab === "appearance" ? <div><span className="eyebrow">TONG LING · PERSONAL</span><h1>個人設定</h1><p>調整你看到的介面外觀，不影響其他人的畫面。</p></div> : <div><span className="eyebrow">TONG LING · HOST CONTROL</span><h1>彤靈工作空間設定</h1><p>管理模型、資料來源與分享存取。這些設定只在主機瀏覽器提供。</p></div>}
            <button className="primary" onClick={() => setSettingsOpen(false)}>返回工作台 →</button>
          </div>
          <div className="settings-page-layout">
          <nav className="settings-page-nav" aria-label="設定分類">
            <div className="settings-tabs"><button className={settingsTab === "appearance" ? "active" : ""} onClick={() => setSettingsTab("appearance")}>外觀與主題</button></div>
            {!isClient && <>
            {[
              ["常用", [["model", "模型連線"], ["knowledge", "知識來源"], ["knowledge_manager", "知識庫管理"],
                        ["output", "PRD 模板"], ["projects", "專案管理"], ["access", "伺服器／客戶端連線"]]],
              ["進階", [["organization", "組織與行業"], ["files", "資料夾與範例"], ["retrieval", "檢索設定"],
                        ["routing", "模型路由"], ["queue", "展示併發"]]],
            ].map(([group, tabs]) => (
              <div className="settings-tabs v7-tabs" key={group as string}>
                <small className="settings-group">{group as string}</small>
                {(tabs as string[][]).map(([key, title]) => (
                  <button
                    key={key}
                    className={settingsTab === key ? "active" : ""}
                    onClick={() => setSettingsTab(key)}
                  >
                    {title}
                  </button>
                ))}
              </div>
            ))}
            </>}
          </nav>
          <section className="settings-page-content" aria-label="設定內容">
            {settingsTab === "appearance" && <AppearanceSettings />}
            {["output", "organization", "retrieval", "routing"].includes(
              settingsTab,
            ) && (
              <V7Settings
                tab={settingsTab}
                config={config}
                setConfig={setConfig}
                act={act}
              />
            )}
            {settingsTab === "projects" && (
              <ProjectManager
                project={project}
                onChanged={projectsChanged}
                act={act}
              />
            )}
            {settingsTab === "knowledge" && <KnowledgeSettings project={project} act={act} onOpenLocal={() => setSettingsTab("knowledge_manager")} />}
            {settingsTab === "knowledge_manager" && <KnowledgeManager config={config} setConfig={setConfig} project={project} act={act} onOpenFolder={() => setSettingsTab("files")} />}
            {settingsTab === "access" && <HostAccess />}
            {settingsTab === "queue" && <QueueSettings onSaved={(value) => {
              const { model_concurrency, ...demo_queue } = value;
              setConfig({ ...config, model_concurrency, demo_queue });
            }} />}
            {!["model", "files"].includes(settingsTab) && (
              <button onClick={() => setSettingsOpen(false)}>關閉設定</button>
            )}
            {settingsTab === "files" && (
              <div className="folder-settings">
                <h3>讓彤靈讀取你的專用資料夾</h3>
                <p>
                  把需求文件放進這個資料夾，即可直接讀取，不必逐份上傳。內容有變更時，再次讀取只會解析新增或異動的檔案。
                </p>
                <label>
                  專用資料夾路徑
                  <input
                    value={workspacePath}
                    disabled={busy || !folderConfig?.can_edit}
                    onChange={(e) => setWorkspacePath(e.target.value)}
                    placeholder="例如 D:\\TongLingFiles"
                  />
                </label>
                {!folderConfig?.can_edit && (
                  <p className="muted">請從執行彤靈的主機修改路徑。</p>
                )}
                <div className="actions">
                  <button
                    disabled={busy || !folderConfig?.can_edit}
                    onClick={() => folderAction("save")}
                  >
                    保存路徑
                  </button>
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => folderAction("scan")}
                  >
                    {busy ? "處理中…" : "讀取資料夾"}
                  </button>
                </div>
                <p className="muted">
                  目前已索引 {folderConfig?.files || 0}{" "}
                  份檔案。讀取後可在「知識檢索」搜尋並加入專案。
                </p>
                {folderStatus && (
                  <div className="feedback" role="status">
                    {folderStatus}
                  </div>
                )}
                <div className="demo-settings">
                  <span className="eyebrow">先體驗，再放入自己的需求</span>
                  <h3>彤靈示範文件包</h3>
                  <p>
                    包含需求說明、規範、SA、三場會議與一場後續變更。刻意保留分歧，方便體驗人工確認及版本修訂。
                  </p>
                  <div className="actions">
                    <button
                      disabled={busy}
                      onClick={() => folderAction("samples")}
                    >
                      建立測試文件到資料夾
                    </button>
                    <a className="button" href="/api/demo/files.zip">
                      下載測試文件
                    </a>
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={applyDemo}
                    >
                      ✦ 一鍵套用 Demo
                    </button>
                  </div>
                  <small>
                    範例全部為虛構資料。重複套用會開啟既有範例專案，保留你的決策。
                  </small>
                </div>
                <button onClick={() => setSettingsOpen(false)}>關閉設定</button>
              </div>
            )}
            <div hidden={settingsTab !== "model"}>
              <label>
                模型供應商
                <select
                  value={config.provider}
                  onChange={(e) =>
                    setConfig({ ...config, provider: e.target.value })
                  }
                >
                  <option value="mock">離線規則抽取（驗證模式）</option>
                  <option value="ollama">Ollama 本機模型</option>
                  <option value="openai_compatible">
                    OpenAI-compatible API
                  </option>
                </select>
              </label>
              <label>
                模型名稱
                <input
                  value={config.model}
                  onChange={(e) =>
                    setConfig({ ...config, model: e.target.value })
                  }
                />
              </label>
              <label>
                API Endpoint
                <input
                  value={config.endpoint}
                  onChange={(e) =>
                    setConfig({ ...config, endpoint: e.target.value })
                  }
                />
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={config.local_only}
                  onChange={(e) =>
                    setConfig({ ...config, local_only: e.target.checked })
                  }
                />
                僅允許本機模型連線
              </label>
              <label>
                本機語意嵌入模型路徑（選用）
                <input
                  value={config.embedding_model_path}
                  onChange={(e) =>
                    setConfig({
                      ...config,
                      embedding_model_path: e.target.value,
                    })
                  }
                />
              </label>
              <p className="muted">
                API 金鑰請在啟動主機設定 OPENAI_API_KEY
                環境變數。金鑰不儲存在瀏覽器。
              </p>
              <ActivityCards tasks={globalTasks} />
              {modelTest && (
                <div
                  role={modelTest.state === "error" ? "alert" : "status"}
                  aria-live="polite"
                  className={modelTest.state === "error" ? "error" : "banner"}
                >
                  {modelTest.state === "loading"
                    ? `測試中 · ${testSeconds} 秒　`
                    : modelTest.state === "success"
                      ? "✓ "
                      : ""}
                  {modelTest.message}
                </div>
              )}
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api("/settings", "PUT", config);
                      setNotice("設定已保存");
                    })
                  }
                >
                  保存設定
                </button>
                <button disabled={busy} onClick={() => testModel()}>
                  保存並測試連線
                </button>
                <button disabled={busy} onClick={() => testModel(true)}>
                  測試實際生成
                </button>
                <button onClick={() => setSettingsOpen(false)}>關閉</button>
              </div>
            </div>
          </section>
          </div>
        </div>
      )}
    </div>
  );
}

// Internal bookkeeping a PM does not need in the activity log.
const HIDDEN_EVENTS = new Set(["RUN", "DELEGATION", "ANALYSIS_STARTED", "MODE", "MEETING_ANALYZING", "DEMO", "DEMO_REVISION"]);
const EVENT_LABELS: Record<string, string> = {
  PROJECT: "專案", KNOWLEDGE: "知識檢索", ANALYSIS: "需求分析", DECISION: "決策",
  AGENT_DECISION: "Agent 決策", PROPOSAL_REVIEW: "提案審閱", PRD: "PRD", PRD_EDIT: "PRD 編修",
  EVIDENCE: "依據", SOURCE_REMOVED: "依據", MEETING: "會議", MEETING_EDIT: "會議",
  MEETING_CONFIRMED: "會議", MEETING_ANALYSIS: "會議", MEETING_MANUAL: "會議", MEETING_ERROR: "會議",
  PRD_DRAFT: "PRD", DIAGRAM: "圖表", BASELINE: "定版", CHANGE: "變更", PAUSE: "暫停", ERROR: "錯誤",
  TECHNICAL_MODEL: "IT 交付", TECHNICAL_DIAGRAMS: "IT 交付", TECHNICAL_AMENDMENT: "IT 交付",
};

const SOURCE_KIND_LABELS: Record<string, string> = {
  REQUIREMENT_TEXT: "需求說明", REQUIREMENT_DOCUMENT: "需求文件", USER_UPLOAD: "一般文件",
  BUSINESS_KNOWLEDGE: "業務知識", INTERNAL_POLICY: "內部規範", TECH_SPEC: "技術規格",
  COMPETITOR: "競品參考", HISTORICAL_PRD: "歷史 PRD", MEETING_RECORD: "會議紀錄",
  EXTERNAL_RESEARCH: "外部研究",
};

function Entry() {
  const [context, setContext] = useState<any>(null);
  const [error, setError] = useState("");
  const [demoError, setDemoError] = useState("");
  const refreshContext = useCallback(async () => {
    try {
      let next = await api("/session/context");
      // The server decides the mode; demo browsers receive their own session.
      if (next.role === "CLIENT" && !next.authenticated && next.demo_available) {
        try {
          await api("/session/demo", "POST");
          next = await api("/session/context");
          setDemoError("");
        } catch (e) {
          setDemoError((e as Error).message);
        }
      }
      setContext(next);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refreshContext();
    const listener = () => void refreshContext();
    window.addEventListener("pair-required", listener);
    return () => window.removeEventListener("pair-required", listener);
  }, [refreshContext]);
  const isClient = context?.role === "CLIENT";
  useEffect(() => {
    if (!isClient) return;
    // Mode switches on the host take effect without a manual reload.
    const timer = window.setInterval(() => void refreshContext(), context?.authenticated ? 30000 : 8000);
    return () => clearInterval(timer);
  }, [isClient, context?.authenticated, refreshContext]);
  if (error) return <p role="alert" className="feedback warning">無法連接彤靈：{error}</p>;
  if (!context) return <p role="status" className="execution-card">正在連接彤靈…</p>;
  if (!isClient) return <App />;
  if (!context.authenticated)
    return <ClientApp mode={context.mode} demoReason={demoError || context.demo_reason} onPaired={() => void refreshContext()} />;
  return <>
    {context.session_kind === "DEMO" && <div className="demo-banner" role="status">展示模式：此瀏覽器使用獨立的展示連線，你的專案與其他人分開；請勿輸入真實客戶資料。</div>}
    <App isClient />
  </>;
}

initAppearanceRuntime();
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Entry />
  </React.StrictMode>,
);
