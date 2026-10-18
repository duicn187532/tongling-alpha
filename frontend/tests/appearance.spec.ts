import { expect, test } from "@playwright/test";

const themes = ["default", "next", "cathay", "scsb", "sinopac", "yuanta"];

test("six palettes switch instantly, persist, and keep readable primary controls", async ({ browser }) => {
  const context = await browser.newContext({ colorScheme: "light" });
  const page = await context.newPage();
  await page.goto("/");
  await page.getByRole("button", { name: "外觀與主題" }).first().click();
  await expect(page.getByRole("heading", { name: "外觀與主題" })).toBeVisible();

  for (const mode of ["light", "dark"] as const) {
    await page.getByRole("radio", { name: mode === "light" ? "淺色" : "深色" }).check();
    for (let i = 0; i < themes.length; i++) {
      await page.getByRole("radio", { name: i === 0 ? "原始配色" : `配色 ${"ABCDE"[i - 1]}` }).check();
      const result = await page.evaluate(() => {
        const root = document.documentElement;
        const style = getComputedStyle(root);
        const rgb = (value: string) => {
          const node = document.createElement("span");
          node.style.color = value;
          document.body.append(node);
          const parsed = getComputedStyle(node).color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
          node.remove();
          return parsed;
        };
        const luminance = (value: string) => rgb(value).map((v) => {
          const x = v / 255;
          return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
        const contrast = (a: string, b: string) => {
          const x = luminance(a), y = luminance(b);
          return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
        };
        return {
          theme: root.dataset.theme,
          scheme: root.dataset.colorScheme,
          primaryContrast: contrast(style.getPropertyValue("--primary"), style.getPropertyValue("--primary-foreground")),
          hoverContrast: contrast(style.getPropertyValue("--primary-hover"), style.getPropertyValue("--primary-hover-foreground")),
          brandContrast: contrast(style.getPropertyValue("--surface"), style.getPropertyValue("--brand-ink")),
          bodyContrast: contrast(style.getPropertyValue("--surface"), style.getPropertyValue("--text")),
          mutedContrast: contrast(style.getPropertyValue("--surface"), style.getPropertyValue("--text-muted")),
        };
      });
      expect(result.theme).toBe(themes[i]);
      expect(result.scheme).toBe(mode);
      expect(result.primaryContrast).toBeGreaterThanOrEqual(4.5);
      expect(result.hoverContrast).toBeGreaterThanOrEqual(4.5);
      expect(result.brandContrast).toBeGreaterThanOrEqual(4.5);
      expect(result.bodyContrast).toBeGreaterThanOrEqual(4.5);
      expect(result.mutedContrast).toBeGreaterThanOrEqual(4.5);
    }
  }

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "yuanta");
  await expect(page.locator("html")).toHaveAttribute("data-color-scheme", "dark");
  const other = await browser.newContext({ colorScheme: "light" });
  const otherPage = await other.newPage();
  await otherPage.goto("/");
  await expect(otherPage.locator("html")).toHaveAttribute("data-theme", "default");
  await other.close();
  await context.close();
});

test("appearance cards fit a phone and system mode follows the device", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  const page = await context.newPage();
  await page.goto("/");
  await page.getByRole("button", { name: "展開專案側欄" }).click();
  await page.getByRole("button", { name: "外觀與主題" }).first().click();
  await expect(page.locator("html")).toHaveAttribute("data-color-scheme", "dark");
  await expect(page.locator(".appearance-card")).toHaveCount(6);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
  await context.close();
});

test("paired client can change appearance without host settings or server writes", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  let writes = 0;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    if (request.method() !== "GET") writes++;
    const path = new URL(request.url()).pathname;
    const body = path.endsWith("/session/context") ? { role: "CLIENT", authenticated: true }
      : path.endsWith("/client/workspace-config") ? {}
      : path.endsWith("/dashboard") ? { projects: [], totals: { projects: 0, running: 0, attention: 0, baselined: 0, open_todos: 0 } }
      : [];
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "外觀與主題" }).first().click();
  await expect(page.getByRole("button", { name: "主機設定" })).toHaveCount(0);
  await page.getByRole("radio", { name: "配色 C" }).check();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "scsb");
  expect(writes).toBe(0);
  await context.close();
});
