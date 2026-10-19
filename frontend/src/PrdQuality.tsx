import { useEffect, useState } from "react";
import { api } from "./api";

type Issue = {
  code: string; severity: "提醒" | "建議" | "資訊"; message: string; suggestion: string;
  requirement_id: string | null; topic: string | null;
};
type Quality = { score: number; counts: Record<string, number>; issues: Issue[]; note: string };
type Part = { title: string; statement: string };

/** Advisory hints on the in-review draft; nothing here blocks the baseline. */
export function QualityPanel({ revision, onChange }: { revision: any; onChange: () => void }) {
  const [quality, setQuality] = useState<Quality | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api<Quality>(`/prd-revisions/${revision.id}/quality`)
      .then((data) => { if (live) { setQuality(data); setError(""); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [revision.id, revision.content_hash]);
  if (revision.status !== "IN_REVIEW") return null;
  if (error) return <p className="feedback warning">品質提示暫時無法讀取：{error}</p>;
  if (!quality) return null;
  const byRequirement = new Map<string, Issue[]>();
  quality.issues.filter((i) => i.requirement_id).forEach((i) => {
    byRequirement.set(i.requirement_id!, [...(byRequirement.get(i.requirement_id!) || []), i]);
  });
  const general = quality.issues.filter((i) => !i.requirement_id);
  const actionable = quality.issues.filter((i) => i.severity !== "資訊").length;
  const rows = (revision.content.requirements || []).filter((r: any) => byRequirement.has(r.id));
  return <details className="card quality-panel" open={actionable > 0}>
    <summary>
      <strong>品質提示</strong> · {quality.score} 分 · {actionable ? `${actionable} 項可改善` : "沒有需要改善的地方"}
      <small className="muted"> · {quality.note}</small>
    </summary>
    {general.map((issue, i) => <p key={i} className="quality-general">{issue.message}。{issue.suggestion}</p>)}
    {rows.map((row: any) => <RequirementHints key={row.id} revision={revision} row={row}
      issues={byRequirement.get(row.id)!} onChange={onChange} />)}
  </details>;
}

function RequirementHints({ revision, row, issues, onChange }: { revision: any; row: any; issues: Issue[]; onChange: () => void }) {
  const [proposal, setProposal] = useState<any>(null);
  const [split, setSplit] = useState<{ split: boolean; reason: string; parts: Part[] } | null>(null);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const fixable = issues.filter((i) => ["VAGUE", "UNMEASURABLE", "NO_ACTOR", "NO_CRITERIA"].includes(i.code));
  const compound = issues.some((i) => i.code === "COMPOUND");
  async function run(name: string, call: () => Promise<void>) {
    setWorking(name);
    setError("");
    try { await call(); } catch (e) { setError((e as Error).message); } finally { setWorking(""); }
  }
  return <div className="quality-item">
    <h4>{row.topic}</h4>
    <ul>
      {issues.map((issue, i) => <li key={i} className={`severity-${issue.severity}`}>
        <span className="severity">{issue.severity}</span> {issue.message}<small className="muted"> · {issue.suggestion}</small>
      </li>)}
    </ul>
    <div className="actions">
      {fixable.length > 0 && <button disabled={!!working} onClick={() => void run("improve", async () => {
        setProposal(await api(`/prd-revisions/${revision.id}/requirements/${row.id}/improve`, "POST",
          { codes: fixable.map((i) => i.code) }, 150000));
      })}>{working === "improve" ? "AI 改寫中…" : "AI 改寫建議"}</button>}
      {compound && <button disabled={!!working} onClick={() => void run("split", async () => {
        setSplit(await api(`/prd-revisions/${revision.id}/requirements/${row.id}/split-suggestion`, "POST", undefined, 150000));
      })}>{working === "split" ? "判斷中…" : "看拆分建議"}</button>}
    </div>
    {proposal && <div className="quality-proposal">
      {proposal.warning && <p className="feedback warning">{proposal.warning}</p>}
      {proposal.statement !== row.statement && <p><strong>建議改寫：</strong>{proposal.statement}</p>}
      {proposal.acceptance_criteria?.length > 0 && <ul>{proposal.acceptance_criteria.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>}
      <div className="actions">
        {(proposal.statement !== row.statement || proposal.acceptance_criteria?.length > 0) && <button className="primary" disabled={!!working}
          onClick={() => void run("apply", async () => {
            if (proposal.statement !== row.statement)
              await api(`/prd-revisions/${revision.id}/requirements/${row.id}`, "PUT", { statement: proposal.statement });
            for (const text of proposal.acceptance_criteria || [])
              await api(`/prd-revisions/${revision.id}/advice/apply`, "POST", { kind: "acceptance_criteria", text, requirement_id: row.id.split("::")[0] });
            setProposal(null);
            onChange();
          })}>採用</button>}
        <button onClick={() => setProposal(null)}>不採用</button>
      </div>
    </div>}
    {split && <div className="quality-proposal">
      {!split.split ? <p className="muted">不建議拆分：{split.reason}</p> : <>
        <p className="muted">{split.reason || "這條需求包含可各自驗收的事項。"}確認或調整後再拆分：</p>
        {split.parts.map((part, i) => <div className="split-part" key={i}>
          <input aria-label={`第 ${i + 1} 條標題`} value={part.title}
            onChange={(e) => setSplit({ ...split, parts: split.parts.map((p, j) => j === i ? { ...p, title: e.target.value } : p) })} />
          <textarea aria-label={`第 ${i + 1} 條需求`} rows={2} value={part.statement}
            onChange={(e) => setSplit({ ...split, parts: split.parts.map((p, j) => j === i ? { ...p, statement: e.target.value } : p) })} />
        </div>)}
      </>}
      <div className="actions">
        {split.split && <button className="primary" disabled={!!working} onClick={() => void run("apply-split", async () => {
          await api(`/prd-revisions/${revision.id}/requirements/${row.id}/split`, "PUT", { parts: split.parts });
          setSplit(null);
          onChange();
        })}>確認拆分</button>}
        <button onClick={() => setSplit(null)}>{split.split ? "維持一條" : "知道了"}</button>
      </div>
    </div>}
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </div>;
}
