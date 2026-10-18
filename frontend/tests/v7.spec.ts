import { test, expect } from "@playwright/test";

test("v7 templates, organization, graph jobs and trash controls persist", async ({
  page,
  request,
}) => {
  const p = await (
    await request.post("/api/projects", {
      data: {
        name: "V7 UI 驗收",
        description: "PaymentAPI 驗證使用者並保存稽核資料",
      },
    })
  ).json();
  await request.post(`/api/projects/${p.id}/workflow/mode`, {
    data: { mode: "MANUAL" },
  });
  await page.goto("/#" + p.id);
  await page.getByRole("button", { name: "設定", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "PRD 模板", exact: true }).click();
  await dialog
    .getByLabel("PRD 模板", { exact: true })
    .selectOption("FINANCIAL_LIGHT");
  await dialog.getByLabel("章節標題 1", { exact: true }).fill("驗收用目標章節");
  await dialog.getByLabel("自訂模板名稱").fill("V7 自訂模板");
  await dialog.getByRole("button", { name: "保存自訂模板並套用" }).click();
  await expect(dialog.getByRole("status")).toContainText("自訂模板已保存");
  await dialog.getByRole("button", { name: "組織與行業", exact: true }).click();
  await dialog.getByLabel("使用者行業（必填）").fill("銀行");
  await dialog
    .getByRole("combobox", { name: "搜尋服務", exact: true })
    .selectOption("google_news");
  await dialog.getByRole("button", { name: "保存此頁設定" }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/settings")).json()).organization
          ?.industry,
    )
    .toBe("銀行");
  await dialog.getByRole("button", { name: "關閉設定", exact: true }).click();
  await page.reload();
  await page.locator(".steps").getByRole("button", { name: /知識/ }).click();
  await page.getByText("標籤、圖譜與同業研究", { exact: true }).click();
  await page.getByLabel("建置範圍").selectOption("project");
  await page
    .getByRole("button", { name: "建立／更新知識圖譜", exact: true })
    .click();
  await expect(
    page
      .locator(".knowledge-tools")
      .getByText(/知識圖譜 · 已完成/)
      .first(),
  ).toBeVisible({ timeout: 30000 });
  await page.getByText(/圖譜總覽/).click();
  await expect(page.getByRole("img", { name: "知識圖譜關係圖" })).toBeVisible();
  await page.screenshot({
    path: "../docs/v7-knowledge-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "設定", exact: true }).click();
  await dialog.getByRole("button", { name: "PRD 模板", exact: true }).click();
  await expect(dialog.getByLabel("章節標題 1", { exact: true })).toHaveValue(
    "驗收用目標章節",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "../docs/v7-settings-mobile.png",
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "專案管理", exact: true }).click();
  await dialog.getByRole("button", { name: "刪除此專案…" }).click();
  await dialog.getByRole("button", { name: "確認移至垃圾桶" }).click();
  await expect(
    dialog.getByRole("button", { name: "復原專案" }).last(),
  ).toBeVisible();
  const trashCard = dialog.locator("article").filter({ hasText: "V7 UI 驗收" });
  await trashCard.getByRole("button", { name: "復原專案" }).click();
  await expect
    .poll(async () => (await request.get("/api/projects/" + p.id)).status())
    .toBe(200);
  await request.delete("/api/projects/" + p.id);
  await dialog.getByRole("button", { name: "關閉設定", exact: true }).click();
  await page.getByRole("button", { name: "設定", exact: true }).click();
  await dialog.getByRole("button", { name: "專案管理", exact: true }).click();
  await trashCard.getByRole("button", { name: "永久刪除…" }).click();
  await expect(
    trashCard.getByRole("button", { name: "確認永久刪除" }),
  ).toBeDisabled();
  await trashCard.getByLabel("永久刪除確認名稱").fill("V7 UI 驗收");
  await trashCard.getByRole("button", { name: "確認永久刪除" }).click();
  await expect(trashCard).toHaveCount(0);
});
