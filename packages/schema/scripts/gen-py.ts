import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const repoRoot = path.resolve(__dirname, '../../..');
const openapiPath = path.join(repoRoot, 'packages/schema/openapi.yaml');
const outputPath = path.join(repoRoot, 'apps/api-gateway/app/generated/schemas.py');

console.log(`[gen-py] openapi -> ${openapiPath}`);
console.log(`[gen-py] out     -> ${outputPath}`);

// 关键修复：使用 --from 指定正确的 PyPI 包名 datamodel-code-generator
const cmd = `uvx --from datamodel-code-generator datamodel-codegen --input "${openapiPath}" --input-file-type openapi --output "${outputPath}" --output-model-type pydantic_v2.BaseModel --target-python-version 3.11 --use-standard-collections --use-schema-description --field-constraints --snake-case-field --wrap-string-literal`;

execSync(cmd, { stdio: 'inherit' });

// 注入 Header 警告信息
const header = `# ============================================================================\n# ⚠️  AUTO-GENERATED FILE — DO NOT EDIT\n# 本文件由 packages/schema/scripts/gen-py.ts 依据 openapi.yaml 生成。\n# 任何类型修改请编辑 openapi.yaml 后重新运行 gen:py。\n# ============================================================================\n\n`;

const content = fs.readFileSync(outputPath, 'utf-8');
fs.writeFileSync(outputPath, header + content, 'utf-8');

console.log('[gen-py] ✅ 生成完成');