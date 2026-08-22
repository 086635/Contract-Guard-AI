/**
 * gen-zod.ts —— 由 openapi.yaml 生成运行时 Zod Schema (openapi-zod-client)
 *
 * 输出：apps/web-ui/src/api/generated/zod.ts
 * 前端 HTTP 响应拦截器使用此处生成的 Zod 进行 SafeParse 运行时断言（§6.2）。
 *
 * ⚠️ AUTO-GENERATED 流程的一部分，禁止手工编辑输出文件。
 *
 * 运行：pnpm --filter @contract-guard/schema gen:zod
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "../../..");
const openapi = resolve(repoRoot, "packages/schema/openapi.yaml");
const out = resolve(repoRoot, "apps/web-ui/src/api/generated/zod.ts");

console.log(`[gen-zod] openapi -> ${openapi}`);
console.log(`[gen-zod] out     -> ${out}`);

execFileSync(
  "npx",
  [
    "openapi-zod-client",
    openapi,
    "--output",
    out,
    "--group-strategy",
    "none",
  ],
  { stdio: "inherit", cwd: repoRoot, shell: true },
);

console.log("[gen-zod] ✅ 生成完成");