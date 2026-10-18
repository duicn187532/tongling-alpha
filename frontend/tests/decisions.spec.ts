import { test, expect } from "@playwright/test";

test("one-click adopt/reject, AI-worded modification, persistence and stale guard", async ({
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
    .getByRole("button", { name: /提案審閱/ })
    .click();
  const card = page.locator(".requirement").first();
  await expect(card.locator(".decision-badge")).toHaveText("尚未決策");
  // Adopting or rejecting needs no reason.
  await card.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(card).toHaveClass(/decision-reject/);
  await card.getByRole("button", { name: "採納", exact: true }).click();
  await expect(card).toHaveClass(/decision-accept/);
  await expect(
    card.getByRole("button", { name: "採納", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  // Modifying: the PM writes a note and the AI words the requirement.
  await card.getByRole("button", { name: "修改…", exact: true }).click();
  await card.getByLabel("你想怎麼改？（AI 會依此整理成正式需求）").fill("改用雙因素驗證");
  await card.getByRole("button", { name: "請 AI 整理", exact: true }).click();
  await expect(card.getByLabel("修改後的需求")).toHaveValue(/雙因素/);
  await card.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(card).toHaveClass(/decision-modify/);
  await page.reload();
  await page
    .locator(".steps")
    .getByRole("button", { name: /提案審閱/ })
    .click();
  await expect(card.locator(".decision-badge")).toHaveText("已修改採納");
  await card.getByRole("button", { name: "稍後確認", exact: true }).click();
  await expect(card).toHaveClass(/decision-pending/);
  const route = `**/api/requirements/${detail.requirements[0].id}/decision`;
  await page.route(route, async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await r.fulfill({ status: 500, json: { detail: "測試保存失敗" } });
  });
  await card.getByRole("button", { name: "採納", exact: true }).click();
  await expect(card.getByRole("status")).toContainText("正在保存");
  await expect(card.getByRole("alert")).toContainText("測試保存失敗");
  await page.unroute(route);
  await card.getByRole("button", { name: "採納", exact: true }).click();
  await expect(card).toHaveClass(/decision-accept/);
  await page.setViewportSize({ width: 390, height: 844 });
  await card.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await request.post(`/api/projects/${p.id}/requirement-text`, {
    data: { text: "通知：改為推播" },
  });
  await expect(
    card.getByRole("button", { name: "採納", exact: true }),
  ).toBeDisabled({ timeout: 10000 });
});
