import { test, expect } from "@playwright/test";

test("create, analyze, dismiss, manual decision, refresh persistence", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page
    .getByRole("button", { name: "＋ 建立新專案", exact: true })
    .click();
  const modal = page.getByRole("dialog", { name: "建立專案" });
  await modal.getByLabel("專案名稱").fill("瀏覽器測試 " + Date.now());
  await modal.getByLabel("需求起點").fill("通知方式：使用電子郵件通知");
  await modal.getByRole("button", { name: "建立專案", exact: true }).click();
  await expect(page.getByRole("button", { name: "繼續處理 →" })).toBeVisible();
  await page.getByRole("button", { name: "繼續處理 →" }).click();
  const decision = page.getByRole("dialog", { name: "人工決策" });
  await expect(decision).toBeVisible();
  await decision.getByRole("button", { name: "稍後處理" }).click();
  await expect(decision).not.toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "查看待辦決策 →" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "手動工作台" }).click();
  await expect(
    page.getByRole("heading", { name: "讓每個決策有依據" }),
  ).toBeVisible();
  const cards = page.locator(".requirement");
  for (let i = 0; i < (await cards.count()); i++) {
    await cards.nth(i).getByLabel("決策理由").fill("確認此需求與來源相符");
    await cards
      .nth(i)
      .getByRole("button", { name: "接受", exact: true })
      .click();
  }
  await page.getByRole("button", { name: "產生待審閱 PRD" }).click();
  await expect(page.getByRole("dialog", { name: "人工決策" })).toBeVisible();
  await page
    .getByRole("dialog", { name: "人工決策" })
    .getByRole("button", { name: "查看內容並處理 →" })
    .click();
  await expect(page.locator(".paper")).toBeVisible();
  await page.getByRole("button", { name: "確認並建立 Baseline" }).click();
  await expect(page.getByRole("status")).toContainText("正式版本已建立", {
    timeout: 60000,
  });
  expect(errors).toEqual([]);
});

for (const width of [390, 430, 768, 1440]) {
  test(`responsive ${width}: composer and decision sheet`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    const p = await (
      await request.post("/api/projects", {
        data: {
          name: "版面測試 " + width,
          description: "資料保存：保存專案資料",
        },
      })
    ).json();
    await request.post(`/api/projects/${p.id}/agent/continue`);
    await page.goto("/");
    await expect(page.getByRole("dialog", { name: "人工決策" })).toBeVisible();
    const bounds = await page
      .getByRole("dialog", { name: "人工決策" })
      .boundingBox();
    expect(bounds!.width).toBeLessThanOrEqual(width);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(845);
    await page.screenshot({
      path: `test-results/mobile-${width}.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "稍後處理", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    await expect(page.getByLabel("補充需求")).toBeVisible();
    await page.getByLabel("補充需求").fill("測試補充文字");
    await expect(page.getByLabel("補充需求")).toHaveValue("測試補充文字");
    await page.screenshot({
      path: `test-results/agent-${width}.png`,
      fullPage: true,
    });
  });
}
