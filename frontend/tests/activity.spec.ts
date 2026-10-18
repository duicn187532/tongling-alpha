import { test, expect } from "@playwright/test";

test("explicit analysis runs again and status survives reload", async ({
  page,
  request,
}) => {
  const p = await (
    await request.post("/api/projects", {
      data: { name: "AI 狀態回歸", description: "登入方式：使用驗證碼" },
    })
  ).json();
  await request.post(`/api/projects/${p.id}/workflow/mode`, {
    data: { mode: "MANUAL" },
  });
  await page.goto("/#" + p.id);
  await page
    .locator(".steps")
    .getByRole("button", { name: /AI 分析/ })
    .click();
  const submitted = page.waitForRequest(
    (r) =>
      r.url().endsWith(`/projects/${p.id}/analysis/run`) &&
      r.method() === "POST",
  );
  await page
    .getByRole("button", { name: "執行／更新分析", exact: true })
    .click();
  await submitted;
  await expect(
    page
      .getByTestId("execution-card")
      .filter({ hasText: "AI 需求分析" })
      .first(),
  ).toContainText("已完成");
  const dialog = page.getByRole("dialog", { name: "人工決策" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "稍後處理" }).click();
  await expect(
    page.getByRole("button", { name: "前往下一步：人工確認" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "執行／更新分析", exact: true })
    .click();
  await expect(
    page.getByTestId("execution-card").filter({ hasText: "AI 需求分析" }),
  ).toHaveCount(2);
  await page.reload();
  await expect(
    page
      .getByTestId("execution-card")
      .filter({ hasText: "AI 需求分析" })
      .first(),
  ).toContainText("已完成");
  await page.screenshot({
    path: "test-results/analysis-feedback.png",
    fullPage: true,
  });
});

test("stale worker and disconnected status are explicit", async ({
  page,
  request,
}) => {
  const p = await (
    await request.post("/api/projects", {
      data: { name: "心跳狀態測試", description: "資料：保存" },
    })
  ).json();
  const base = await (await request.get(`/api/projects/${p.id}`)).json();
  let offline = false;
  await page.route(`**/api/projects/${p.id}`, async (route) => {
    if (offline) return route.abort("failed");
    await route.fulfill({
      json: {
        ...base,
        runtime: "RUNNING",
        activities: [
          {
            id: "slow-test",
            title: "AI 需求分析",
            status: "RUNNING",
            phase: "模型生成中",
            detail: "來源 1/2：需求文件",
            model: "測試模型",
            created_at: new Date(Date.now() - 70000).toISOString(),
            heartbeat_at: new Date(Date.now() - 25000).toISOString(),
            updated_at: new Date(Date.now() - 65000).toISOString(),
          },
        ],
      },
    });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#" + p.id);
  await expect(page.getByTestId("execution-card")).toContainText(
    "未收到工作心跳",
  );
  await expect(page.getByTestId("execution-card")).toContainText("模型生成中");
  await expect(page.getByTestId("execution-card")).toContainText("耗時較久");
  await page.screenshot({
    path: "test-results/activity-mobile.png",
    fullPage: true,
  });
  offline = true;
  await page.getByRole("button", { name: "更新狀態", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Agent 狀態檢查" }),
  ).toContainText("無法取得最新狀態");
});
