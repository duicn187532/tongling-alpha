import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests",
  timeout: 60000,
  use: {
    baseURL: process.env.TONGLING_TEST_URL || "http://127.0.0.1:8897",
    headless: true,
  },
  workers: 1,
  reporter: "list",
});
