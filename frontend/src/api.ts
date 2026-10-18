export async function api<T = any>(
  path: string,
  method = "GET",
  body?: unknown,
  timeoutMs?: number,
): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch("/api" + path, {
    method,
    credentials: "same-origin",
    headers: body && !form ? { "Content-Type": "application/json" } : {},
    body: body ? (form ? body : JSON.stringify(body)) : undefined,
    signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined,
  }).catch((error: Error) => {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      throw new Error("測試等待逾時，請確認模型服務狀態後重試。");
    }
    throw error;
  });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    if (response.status === 401)
      window.dispatchEvent(new Event("pair-required"));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : JSON.stringify(error.detail),
    );
  }
  return response.json();
}
