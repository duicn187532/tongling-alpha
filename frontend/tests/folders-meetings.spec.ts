import { test, expect } from "@playwright/test";
import path from "node:path";

test("settings feedback, dedicated folder and file-backed demo", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "設定", exact: true }).click();
  const settings = page.getByRole("dialog", { name: "模型設定" });
  await settings.getByRole("button", { name: "保存並測試連線" }).click();
  await expect(settings.getByRole("status")).toContainText("離線規則模式可用");
  await settings.getByRole("button", { name: "資料夾與範例" }).click();
  const folder = path.resolve("..", "..", "..", "work", "browser-demo-folder");
  await settings.getByLabel("專用資料夾路徑").fill(folder);
  await settings.getByRole("button", { name: "保存路徑", exact: true }).click();
  await expect(settings.getByRole("status")).toContainText("專用資料夾已保存");
  await settings.getByRole("button", { name: "建立測試文件到資料夾" }).click();
  await expect(settings.getByRole("status")).toContainText("測試文件已放入");
  await settings
    .getByRole("button", { name: "讀取資料夾", exact: true })
    .click();
  await expect(settings.getByRole("status")).toContainText("讀取完成");
  await page.screenshot({
    path: "test-results/folder-settings.png",
    fullPage: true,
  });
  await settings
    .getByRole("button", { name: "✦ 一鍵套用 Demo", exact: true })
    .click();
  await expect(settings).not.toBeVisible();
  await expect(page.locator(".meeting-card")).toHaveCount(3);
  await expect(page.locator(".demo-banner")).toContainText("虛構測試資料");
  const meeting = page.locator(".meeting-card").first();
  if (
    !(await meeting.locator(".meeting-state").innerText()).includes("已確認")
  ) {
    await meeting
      .getByRole("button", { name: "分析會議紀錄", exact: true })
      .click();
    await expect(meeting.locator(".meeting-state")).toContainText("分析完成", {
      timeout: 15000,
    });
    await expect(
      meeting.getByRole("button", { name: /確認這.*項紀錄/ }),
    ).toBeEnabled();
    await meeting.getByRole("button", { name: /確認這.*項紀錄/ }).click();
  }
  await expect(meeting.locator(".meeting-state")).toContainText("已確認");
  await page.screenshot({
    path: "test-results/demo-meetings.png",
    fullPage: true,
  });
});

test("empty meeting gives visible feedback and manual repair", async ({
  page,
  request,
}) => {
  const p = await (
    await request.post("/api/projects", {
      data: { name: "會議補編測試", description: "測試會議修復" },
    })
  ).json();
  await request.post(`/api/projects/${p.id}/workflow/mode`, {
    data: { mode: "MANUAL" },
  });
  await request.post(`/api/projects/${p.id}/meetings`, {
    data: { title: "待補充紀錄", meeting_date: "2026-10-15", transcript: "好" },
  });
  await page.goto("/#" + p.id);
  await page
    .locator(".steps")
    .getByRole("button", { name: /會議助理/ })
    .click();
  const card = page.locator(".meeting-card").first();
  await card.getByRole("button", { name: "分析會議紀錄", exact: true }).click();
  await expect(card.locator(".meeting-state")).toContainText("內容不足");
  await expect(card.getByRole("alert")).toContainText("沒有找到可確認");
  await expect(
    card.getByRole("button", { name: "確認這 0 項紀錄" }),
  ).toBeDisabled();
  await card.getByText("人工整理紀錄（不必等待模型）", { exact: true }).click();
  await card
    .getByLabel("人工會議紀錄", { exact: true })
    .fill("驗證方式：決議使用裝置綁定加一次性驗證碼。");
  await card.getByRole("button", { name: "保存人工紀錄" }).click();
  await card.getByRole("button", { name: "確認這 1 項紀錄" }).click();
  await expect(card.locator(".meeting-state")).toContainText("已確認");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/meeting-mobile.png",
    fullPage: true,
  });
});
