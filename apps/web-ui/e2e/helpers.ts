import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Download, type Page } from "@playwright/test";

export const ADMIN = {
  username: process.env.CG_E2E_ADMIN_USERNAME ?? "e2e-admin",
  password: process.env.CG_E2E_ADMIN_PASSWORD ?? "e2e-admin-pass",
};

export const VIEWER = {
  username: process.env.CG_E2E_VIEWER_USERNAME ?? "e2e-viewer",
  password: process.env.CG_E2E_VIEWER_PASSWORD ?? "e2e-viewer-pass",
};

const FIXTURES = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures");

export const fixturePath = (name: string): string => path.join(FIXTURES, name);

/** 真实 UI 登录（本地账号 + refresh cookie）。 */
export async function login(page: Page, who: "admin" | "viewer" = "admin"): Promise<void> {
  const account = who === "admin" ? ADMIN : VIEWER;
  await page.goto("/login");
  await page.getByLabel("用户名").fill(account.username);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/contracts/, { timeout: 30_000 });
}

export async function logout(page: Page): Promise<void> {
  await page.getByLabel("退出登录").click();
  await expect(page).toHaveURL(/\/login/, { timeout: 30_000 });
}

/** 走真实上传面板：选择文件 → 开始解析 → 等终态 → 进入审核页，返回 contractId。 */
export async function uploadAndEnterAudit(page: Page, fixture: string): Promise<string> {
  await page.goto("/contracts?upload=1");
  await page.locator('input[type="file"]').setInputFiles(fixturePath(fixture));
  await page.getByRole("button", { name: "开始解析" }).click();
  await expect(page.getByText("已完成解析")).toBeVisible({ timeout: 240_000 });
  await page.getByRole("button", { name: "进入审核工作台" }).click();
  await expect(page).toHaveURL(/\/contracts\/[0-9a-f-]+\/audit/, { timeout: 30_000 });
  const match = /\/contracts\/([0-9a-f-]+)\/audit/.exec(page.url());
  if (!match) throw new Error(`未从 URL 解析出 contractId：${page.url()}`);
  return match[1];
}

/** 断言下载文件魔数（DOCX=PK\x03\x04，PDF=%PDF-）。 */
export async function expectDownloadMagic(download: Download, magic: number[]): Promise<void> {
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of stream) {
    const buffer = Buffer.from(chunk);
    chunks.push(buffer);
    length += buffer.length;
    if (length >= magic.length) break;
  }
  expect([...Buffer.concat(chunks).subarray(0, magic.length)]).toEqual(magic);
}

export async function openReportPage(page: Page, contractId: string): Promise<void> {
  await page.goto(`/contracts/${contractId}/report`);
  const generate = page.getByRole("button", { name: "生成报告", exact: true });
  if ((await generate.count()) > 0) {
    await generate.click();
  }
}
