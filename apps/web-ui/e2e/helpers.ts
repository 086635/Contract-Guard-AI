import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
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

const MIME_TYPES: Record<string, string> = {
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

/** 为每次上传写入唯一但格式合法的标记，避免真实栈 file_hash 唯一约束阻断后续链路。 */
function uniqueFixtureBytes(name: string, source: Buffer): Buffer {
  const marker = `C75-${randomUUID()}`;
  const lower = name.toLowerCase();
  if (lower.endsWith(".pdf")) {
    return Buffer.concat([source, Buffer.from(`\n% ${marker}\n`, "ascii")]);
  }
  if (lower.endsWith(".docx")) {
    const eocdSignature = Buffer.from([0x50, 0x4b, 0x05, 0x06]);
    const eocd = source.lastIndexOf(eocdSignature);
    if (eocd < 0) throw new Error(`DOCX 缺少 EOCD 记录：${name}`);
    const comment = Buffer.from(marker, "utf8");
    const eocdRecord = Buffer.from(source.subarray(eocd, eocd + 22));
    eocdRecord.writeUInt16LE(comment.length, 20);
    return Buffer.concat([source.subarray(0, eocd), eocdRecord, comment]);
  }
  return source;
}

/** 选择真实夹具文件；同名夹具的字节内容在每次调用时唯一。 */
export async function selectFixture(page: Page, fixture: string): Promise<void> {
  const suffix = path.extname(fixture).toLowerCase();
  const mimeType = MIME_TYPES[suffix];
  if (!mimeType) throw new Error(`不支持的 E2E 夹具：${fixture}`);
  const source = await readFile(fixturePath(fixture));
  await page.locator('input[type="file"]').setInputFiles({
    name: fixture,
    mimeType,
    buffer: uniqueFixtureBytes(fixture, source),
  });
}

/**
 * nginx 对 `/api/v1/auth/login` 限流 5 次/分（C68 生产安全策略，**不得为验收关闭**）。
 * workers=1 只保证串行，挡不住「若干短链路连续登录」的突发；这里按最小间隔节流，
 * 让 13 条链路的登录总量稳定落在限流预算内，避免 503 假红（C75-B 避坑 #2）。
 */
const LOGIN_MIN_INTERVAL_MS = 13_000;
let lastLoginAt = 0;

async function respectLoginRateLimit(): Promise<void> {
  const waitMs = lastLoginAt + LOGIN_MIN_INTERVAL_MS - Date.now();
  if (waitMs > 0) {
    await new Promise((resolve) => setTimeout(resolve, waitMs));
  }
  lastLoginAt = Date.now();
}

/** 真实 UI 登录（本地账号 + refresh cookie）。 */
export async function login(page: Page, who: "admin" | "viewer" = "admin"): Promise<void> {
  const account = who === "admin" ? ADMIN : VIEWER;
  await respectLoginRateLimit();
  await page.goto("/login");
  await page.getByLabel("用户名").fill(account.username);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/contracts/, { timeout: 30_000 });
  await expect(page.getByLabel("退出登录")).toBeVisible({ timeout: 30_000 });
}

export async function logout(page: Page): Promise<void> {
  await page.getByLabel("退出登录").click();
  await expect(page).toHaveURL(/\/login/, { timeout: 30_000 });
}

/** 审核任务在后台继续跑：反复刷新审核页，直到目标风险动作出现。 */
export async function refreshUntilVisible(
  page: Page,
  locator: ReturnType<Page["locator"]>,
  timeoutMs = 360_000,
): Promise<void> {
  const refresh = page.getByRole("button", { name: "刷新" });
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const target = locator.first();
    if ((await target.count()) > 0 && (await target.isVisible().catch(() => false))) return;
    if (await refresh.isEnabled().catch(() => false)) {
      await refresh.click();
    }
    await page.waitForTimeout(2_500);
  }
  await expect(locator.first()).toBeVisible({ timeout: 10_000 });
}

/** 走真实上传面板：选择文件 → 开始解析 → 等终态 → 进入审核页，返回 contractId。 */
export async function uploadAndEnterAudit(page: Page, fixture: string): Promise<string> {
  await page.goto("/contracts?upload=1");
  await selectFixture(page, fixture);
  await page.getByRole("button", { name: "开始解析" }).click();
  await expect(page.getByText("已完成解析", { exact: true })).toBeVisible({ timeout: 240_000 });
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
  await expect(page.getByRole("heading", { name: "审核报告与导出中心" })).toBeVisible({ timeout: 30_000 });
  const generate = page.getByRole("button", { name: "生成报告", exact: true });
  const download = page.getByRole("button", { name: /下载 (Word|PDF)/ });
  // ⚠️ 报告未生成时下载按钮**仍然渲染但 disabled**（Report.tsx `canDownload*`），
  // 因此不能用 `isVisible()` 判定「报告已就绪」——必须用 `isEnabled()`；
  // 否则会在 loading 态抢先返回、漏点「生成报告」，让下载按钮永远 disabled（C75-B 链路 9/10 实测）。
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await download.first().isEnabled().catch(() => false)) return;
    if (await generate.isVisible().catch(() => false)) {
      await generate.click();
      return;
    }
    await page.waitForTimeout(1_000);
  }
  throw new Error("报告页既未出现可下载按钮，也未出现「生成报告」按钮");
}
