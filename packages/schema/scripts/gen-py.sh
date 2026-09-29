#!/usr/bin/env bash
# ============================================================================
# gen-py.sh —— 由 openapi.yaml 生成后端 Pydantic DTO (datamodel-code-generator)
#
# 输出：apps/api-gateway/app/generated/schemas.py
# FastAPI 路由仅 import 该文件中的模型，禁止手写 DTO（§6.1 SSOT 派生）。
#
# ⚠️ AUTO-GENERATED 流程的一部分，禁止手工编辑输出文件。
#
# 运行：pnpm --filter @contract-guard/schema gen:py
# 前置：uv 已安装；datamodel-code-generator 在 dev 依赖或 uv tool 中可用
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OPENAPI="$REPO_ROOT/packages/schema/openapi.yaml"
OUT="$REPO_ROOT/apps/api-gateway/app/generated/schemas.py"

echo "[gen-py] openapi -> $OPENAPI"
echo "[gen-py] out     -> $OUT"

uvx datamodel-codegen \
  --input "$OPENAPI" \
  --input-file-type openapi \
  --output "$OUT" \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.11 \
  --use-standard-collections \
  --use-schema-description \
  --field-constraints \
  --snake-case-field \
  --wrap-string-literal \
  --disable-timestamp

# 注入文件头警告（即便生成器未加）
TMP="$(mktemp)"
{
  echo "# ============================================================================"
  echo "# ⚠️  AUTO-GENERATED FILE — DO NOT EDIT"
  echo "# 本文件由 packages/schema/scripts/gen-py.sh 依据 openapi.yaml 生成。"
  echo "# 任何类型修改请编辑 openapi.yaml 后重新运行 gen:py。"
  echo "# ============================================================================"
  cat "$OUT"
} > "$TMP"
mv "$TMP" "$OUT"

echo "[gen-py] ✅ 生成完成"
