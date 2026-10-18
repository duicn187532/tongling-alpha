import { test, expect } from "@playwright/test";

test("decision feedback, colors, retry, persistence and stale guard", async ({
  page,
  request,
}) => {
  const p = await (
    await request.post("/api/projects", {
      data: { name: "人工確認狀態測試", description: "登入方式：使用驗證碼" },
    })
  ).json();
  await request.post(`/api/projects/${p.id}/workflow/mode`, {
    data: { mode: "MANUAL" },
  });
  await request.post(`/api/projects/${p.id}/analysis/run`);
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/projects/${p.id}`)).json()).runtime,
    )
    .toBe("WAITING_HUMAN");
  const detail = await (await request.get(`/api/projects/${p.id}`)).json();
  for (const i of detail.interruptions)
    await request.post(`/api/interruptions/${i.id}/dismiss`);
  await page.goto("/#" + p.id);
  await page
    .locator(".steps")
    .getByRole("button", { name: /人工確認/ })
    .click();
  const card = page.locator(".requirement").first();
  await expect(card.locator(".decision-badge")).toHaveText("尚未決策");
  await expect(
    page.getByRole("button", { name: "產生待審閱 PRD", exact: true }),
  ).toBeDisabled();
  await card.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(card.getByRole("alert")).toContainText("請先填寫決策理由");
  await card.getByRole("button", { name: "接受", exact: true }).click();
  await expect(card).toHaveClass(/decision-accept/);
  await expect(
    card.getByRole("button", { name: "接受", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(card.getByRole("status")).toContainText("保存");
  await expect(page.locator(".review-summary")).toContainText(
    "已完成 1 / 1 項",
  );
  await expect(
    page.getByRole("button", { name: "產生待審閱 PRD", exact: true }),
  ).toBeEnabled();
  await card.getByLabel("修改後的需求").fill("登入必須使用雙因素驗證");
  await card.getByLabel("決策理由").fill("人工確認安全要求");
  await page.waitForTimeout(2300); // Ensure background polling does not discard unsaved edits.
  await expect(card.getByLabel("修改後的需求")).toHaveValue(
    "登入必須使用雙因素驗證",
  );
  await card.getByRole("button", { name: "採用修改", exact: true }).click();
  await expect(card).toHaveClass(/decision-modify/);
  await page.reload();
  await page
    .locator(".steps")
    .getByRole("button", { name: /人工確認/ })
    .click();
  await expect(card.locator(".decision-badge")).toHaveText("已採用修改");
  await expect(card.getByLabel("修改後的需求")).toHaveValue(
    "登入必須使用雙因素驗證",
  );
  await card.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(card).toHaveClass(/decision-reject/);
  await expect(card.locator(".decision-saved")).toContainText("不納入 PRD");
  await card.getByRole("button", { name: "待確認", exact: true }).click();
  await expect(card).toHaveClass(/decision-pending/);
  await expect(page.locator(".review-summary")).toContainText(
    "已完成 0 / 1 項",
  );
  await expect(card.getByRole("status")).toContainText("尚未完成");
  const route = `**/api/requirements/${detail.requirements[0].id}/decision`;
  await page.route(route, async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await r.fulfill({ status: 500, json: { detail: "測試保存失敗" } });
  });
  await card.getByRole("button", { name: "接受", exact: true }).click();
  await expect(card.getByRole("status")).toContainText("正在保存");
  await expect(card.getByRole("alert")).toContainText("測試保存失敗");
  await expect(card.locator(".decision-badge")).toHaveText("待確認");
  await page.unroute(route);
  await card.getByRole("button", { name: "接受", exact: true }).click();
  await expect(card).toHaveClass(/decision-accept/);
  await page.setViewportSize({ width: 390, height: 844 });
  await card.scrollIntoViewIfNeeded();
  await card.screenshot({ path: "test-results/decision-mobile.png" });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await request.post(`/api/projects/${p.id}/requirement-text`, {
    data: { text: "通知：改為推播" },
  });
  await expect(
    card.getByRole("button", { name: "接受", exact: true }),
  ).toBeDisabled();
  await expect(page.locator(".review-summary")).toContainText(
    "請先更新 AI 分析",
  );
});
