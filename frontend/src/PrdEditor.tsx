import { useEffect, useState } from "react";
import { api } from "./api";

type Gap = { field: string; title: string; question: string; filled: boolean; items: string[] };

/** Lists empty PRD sections as questions; answers are worded by AI and saved into the draft. */
export function GapPanel({ revision, onChange }: { revision: any; onChange: (revision: any) => void }) {
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const editable = revision.status === "IN_REVIEW";
  useEffect(() => {
    let live = true;
    api<{ gaps: Gap[] }>(`/prd-revisions/${revision.id}/gaps`)
      .then((data) => { if (live) setGaps(data.gaps); })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [revision.id, revision.content_hash]);
  async function run(field: string, call: () => Promise<any>) {
    setWorking(field);
    setError("");
    try {
      const result = await call();
      onChange(result.revision || result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorking("");
    }
  }
  const open = gaps.filter((gap) => !gap.filled);
  const filled = gaps.filter((gap) => gap.filled);
  if (!editable) return null;
  return <section className="card gap-panel" aria-label="補齊 PRD">
    <div className="row"><h3>補齊 PRD</h3><span className="badge">{open.length ? `還有 ${open.length} 個章節待補` : "主要章節都已填寫"}</span></div>
    <p className="muted">回答下面的問題，AI 只會依你的回答整理成 PRD 條列；不知道的可以先略過，定版不會被擋住。</p>
    {open.map((gap) => <div className="gap-item" key={gap.field}>
      <label><strong>{gap.title}</strong> · {gap.question}
        <textarea rows={2} aria-label={`${gap.title}：${gap.question}`} value={answers[gap.field] || ""} onChange={(e) => setAnswers({ ...answers, [gap.field]: e.target.value })} />
      </label>
      <button className="primary" disabled={!!working || !(answers[gap.field] || "").trim()}
        onClick={() => void run(gap.field, async () => {
          const result = await api(`/prd-revisions/${revision.id}/gaps/${gap.field}/answer`, "POST", { answer: answers[gap.field] }, 120000);
          setAnswers({ ...answers, [gap.field]: "" });
          return result;
        })}>{working === gap.field ? "整理中…" : "交給 AI 整理並寫入"}</button>
    </div>)}
    {filled.length > 0 && <details>
      <summary>已填寫的章節 · {filled.length}（可編修）</summary>
      {filled.map((gap) => <div className="gap-item" key={gap.field}>
        <strong>{gap.title}</strong>
        {editing[gap.field] === undefined
          ? <><ul>{gap.items.map((item, i) => <li key={i}>{item}</li>)}</ul>
            <button className="text-button" onClick={() => setEditing({ ...editing, [gap.field]: gap.items.join("\n") })}>編修</button></>
          : <>
            <label>每行一條<textarea rows={4} value={editing[gap.field]} onChange={(e) => setEditing({ ...editing, [gap.field]: e.target.value })} /></label>
            <div className="actions">
              <button className="primary" disabled={!!working} onClick={() => void run(gap.field, async () => {
                const result = await api(`/prd-revisions/${revision.id}/sections/${gap.field}`, "PUT", { items: editing[gap.field].split("\n") });
                const next = { ...editing };
                delete next[gap.field];
                setEditing(next);
                return result;
              })}>保存</button>
              <button onClick={() => { const next = { ...editing }; delete next[gap.field]; setEditing(next); }}>取消</button>
            </div>
          </>}
      </div>)}
    </details>}
    <TextFields revision={revision} working={working} run={run} />
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </section>;
}

function TextFields({ revision, working, run }: { revision: any; working: string; run: (field: string, call: () => Promise<any>) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [background, setBackground] = useState(revision.content.background || "");
  const [goals, setGoals] = useState(revision.content.goals || "");
  useEffect(() => { setBackground(revision.content.background || ""); setGoals(revision.content.goals || ""); }, [revision.id]);
  if (!open) return <button className="text-button" onClick={() => setOpen(true)}>編修背景與目標</button>;
  return <div className="gap-item">
    <label>背景<textarea rows={3} value={background} onChange={(e) => setBackground(e.target.value)} /></label>
    <label>目標<textarea rows={2} value={goals} onChange={(e) => setGoals(e.target.value)} /></label>
    <div className="actions">
      <button className="primary" disabled={!!working} onClick={() => void run("text", async () => {
        await api(`/prd-revisions/${revision.id}/sections/background`, "PUT", { text: background });
        const result = await api(`/prd-revisions/${revision.id}/sections/goals`, "PUT", { text: goals });
        setOpen(false);
        return result;
      })}>保存</button>
      <button onClick={() => setOpen(false)}>取消</button>
    </div>
  </div>;
}

/** Inline wording, priority and role edits on one requirement row of the draft. */
export function RequirementRowEditor({ revision, row, onChange }: { revision: any; row: any; onChange: (revision: any) => void }) {
  const [open, setOpen] = useState(false);
  const [statement, setStatement] = useState(row.description);
  const [priority, setPriority] = useState(row.priority || "待確認");
  const [actor, setActor] = useState(row.actor === "待確認" ? "" : row.actor || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  if (revision.status !== "IN_REVIEW" || !row.requirement_id) return null;
  const parent = String(row.requirement_id).includes("::") ? String(row.requirement_id).split("::")[0] : "";
  if (!open) return <span className="row-actions">
    <button className="text-button" onClick={() => { setStatement(row.description); setOpen(true); }}>編修</button>
    {parent && <button className="text-button" disabled={saving} onClick={async () => {
      setSaving(true);
      try { onChange(await api(`/prd-revisions/${revision.id}/requirements/${parent}/split`, "PUT", { parts: [] })); }
      catch (e) { setError((e as Error).message); }
      finally { setSaving(false); }
    }}>取消拆分</button>}
    {error && <small role="alert" className="feedback warning">{error}</small>}
  </span>;
  return <div className="gap-item">
    <label>需求<textarea rows={3} value={statement} onChange={(e) => setStatement(e.target.value)} /></label>
    <div className="row">
      <label>優先級<select value={priority} onChange={(e) => setPriority(e.target.value)}>
        {["高", "中", "低", "待確認"].map((p) => <option key={p}>{p}</option>)}
      </select></label>
      <label>角色<input value={actor} placeholder="例如：企業客戶管理者、員工" onChange={(e) => setActor(e.target.value)} /></label>
    </div>
    <div className="actions">
      <button className="primary" disabled={saving || !statement.trim()} onClick={async () => {
        setSaving(true);
        setError("");
        try {
          onChange(await api(`/prd-revisions/${revision.id}/requirements/${row.requirement_id}`, "PUT", { statement, priority, actor }));
          setOpen(false);
        } catch (e) {
          setError((e as Error).message);
        } finally {
          setSaving(false);
        }
      }}>{saving ? "保存中…" : "保存"}</button>
      <button onClick={() => setOpen(false)}>取消</button>
    </div>
    {error && <p role="alert" className="feedback warning">{error}</p>}
  </div>;
}
