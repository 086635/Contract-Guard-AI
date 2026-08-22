/**
 * gen-ts.ts —— 由 openapi.yaml 生成前端 TS 类型声明 (openapi-typescript)
 *
 * 输出：apps/web-ui/src/api/generated/types.ts
 * ⚠️ AUTO-GENERATED 流程的一部分，禁止手工编辑输出文件。
 *
 * 运行：pnpm --filter @contract-guard/schema gen:ts
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "../../..");
const openapi = resolve(repoRoot, "packages/schema/openapi.yaml");
const out = resolve(repoRoot, "apps/web-ui/src/api/generated/types.ts");

console.log(`[gen-ts] openapi -> ${openapi}`);
console.log(`[gen-ts] out     -> ${out}`);

execFileSync(
  "npx",
  [
    "openapi-typescript",
    openapi,
    "--output",
    out,
    "--alphabetize",
    "--export-type",
  ],
  { stdio: "inherit", cwd: repoRoot, shell: true },
);

console.log("[gen-ts] ✅ 生成完成");