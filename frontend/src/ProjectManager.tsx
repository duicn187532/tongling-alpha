import { useEffect, useState } from "react";
import { api } from "./api";
export function ProjectManager({
  project,
  onChanged,
  act,
}: {
  project: any;
  onChanged: (deleted?: string) => Promise<void>;
  act: (f: () => Promise<unknown>, m?: string) => Promise<void>;
}) {
  const [trash, setTrash] = useState<any[]>([]),
    [confirm, setConfirm] = useState(""),
    [purging, setPurging] = useState(""),
    [name, setName] = useState(""),
    [message, setMessage] = useState("");
  async function refresh() {
    setTrash(await api("/projects/trash"));
  }
  useEffect(() => {
    refresh().catch((e) => setMessage(e.message));
  }, []);
  return (
    <section>
      <h3>專案與垃圾桶</h3>
      <p>
        刪除會先移至垃圾桶，停止背景工作並從檢索排除；可以復原。永久刪除只清除專案專屬資料，保留共用知識。
      </p>
      {project && (
        <div className="card">
          <strong>目前專案：{project.name}</strong>
          {confirm !== project.id ? (
            <button onClick={() => setConfirm(project.id)}>刪除此專案…</button>
          ) : (
            <div className="feedback warning">
              <p>確定將「{project.name}」移至垃圾桶？</p>
              <button
                onClick={() =>
                  act(async () => {
                    await api("/projects/" + project.id, "DELETE");
                    await onChanged(project.id);
                    setConfirm("");
                    await refresh();
                    setMessage("專案已移至垃圾桶。");
                  })
                }
              >
                確認移至垃圾桶
              </button>
              <button onClick={() => setConfirm("")}>取消</button>
            </div>
          )}
        </div>
      )}
      <h4>垃圾桶（{trash.length}）</h4>
      {trash.map((p) => (
        <article className="card" key={p.id}>
          <h4>{p.name}</h4>
          <small>刪除時間：{new Date(p.deleted_at).toLocaleString()}</small>
          <div className="actions">
            <button
              onClick={() =>
                act(async () => {
                  await api(`/projects/${p.id}/restore`, "POST");
                  await refresh();
                  await onChanged();
                  setMessage("專案已復原；背景工作需手動恢復。");
                })
              }
            >
              復原專案
            </button>
            <button
              onClick={() => {
                setPurging(p.id);
                setName("");
              }}
            >
              永久刪除…
            </button>
          </div>
          {purging === p.id && (
            <div className="feedback warning">
              <p>永久刪除無法復原。請輸入完整專案名稱「{p.name}」再次確認。</p>
              <input
                aria-label="永久刪除確認名稱"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <button
                disabled={name !== p.name}
                onClick={() =>
                  act(async () => {
                    const result = await api(
                      `/projects/${p.id}/permanent`,
                      "DELETE",
                      { confirmation: name },
                    );
                    await refresh();
                    setPurging("");
                    setMessage(
                      result.file_cleanup_errors.length
                        ? "資料庫已清除，部分匯出檔刪除失敗，請檢查檔案是否被開啟。"
                        : "專案已永久清除。",
                    );
                  })
                }
              >
                確認永久刪除
              </button>
              <button onClick={() => setPurging("")}>取消</button>
            </div>
          )}
        </article>
      ))}
      {message && (
        <p role="status" className="feedback success">
          {message}
        </p>
      )}
    </section>
  );
}
