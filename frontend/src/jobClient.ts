import { api } from "./api";

export type Job = {
  job_id: string; kind: string; project_id: string | null;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  progress: number; error: string; cancel_requested: number; created_at: string;
  finished_at?: string | null; dismissed_at?: string | null;
  result?: any;
};

export async function submitAndWait(kind: "GUIDANCE" | "RAG" | "PRD", payload: Record<string, unknown>, onStatus?: (job: Job) => void) {
  let job = await api<Job>("/jobs", "POST", { kind, ...payload });
  onStatus?.(job);
  while (job.status === "QUEUED" || job.status === "RUNNING") {
    await new Promise((resolve) => window.setTimeout(resolve, 3000));
    job = await api<Job>(`/jobs/${job.job_id}`, "GET", undefined, 10000);
    onStatus?.(job);
  }
  if (job.status !== "COMPLETED") throw new Error(job.error || (job.status === "CANCELLED" ? "工作已取消" : "工作執行失敗"));
  return job.result;
}
