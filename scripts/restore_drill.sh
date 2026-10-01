#!/usr/bin/env bash
# =============================================================================
# C69 · 备份恢复演练（T-D7-8）
#
# 用法（默认 dry-run，必须显式 --execute 才会写目标）：
#   scripts/restore_drill.sh --pg-backup <dump|dir> --minio-backup <dir> \
#       [--target-dsn <dsn>] [--target-alias <mc-alias>] [--target-bucket <bucket>] \
#       [--report <dir>] [--execute]
#
# 环境变量（CLI 优先）：
#   CG_RESTORE_PG_BACKUP       PG 备份文件或目录
#   CG_RESTORE_MINIO_BACKUP    MinIO 备份目录（由 backup_minio.sh 产出）
#   CG_RESTORE_TARGET_DSN      恢复目标 PG 连接串（--execute 必填）
#   CG_RESTORE_TARGET_ALIAS    恢复目标 mc alias（--execute 必填）
#   CG_RESTORE_TARGET_BUCKET   恢复目标 bucket（--execute 必填）
#   CG_RESTORE_REPORT_DIR      演练报告目录（默认与 PG 备份同目录）
#   CG_PG_RESTORE_BIN / CG_PSQL_BIN / CG_MINIO_MC_BIN  可执行文件覆盖
#
# 退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 恢复命令失败 / 5 校验失败 / 6 I/O 失败
#
# 校验链：
#   1) dump 的 sha256 与 .meta 记录一致（存在 .meta 时）
#   2) pg_restore --list 可读
#   3) 恢复后 alembic_version 与备份 .meta 记录一致
#   4) MinIO 目标 bucket 的 "<size>\t<key>" 清单与备份 objects.tsv 完全一致
# =============================================================================
set -euo pipefail

PROG="${0##*/}"
EXIT_OK=0
EXIT_USAGE=2
EXIT_DEP=3
EXIT_CMD=4
EXIT_VERIFY=5
EXIT_IO=6

PG_RESTORE_BIN="${CG_PG_RESTORE_BIN:-pg_restore}"
PSQL_BIN="${CG_PSQL_BIN:-psql}"
MC_BIN="${CG_MINIO_MC_BIN:-mc}"

PG_BACKUP="${CG_RESTORE_PG_BACKUP:-}"
MINIO_BACKUP="${CG_RESTORE_MINIO_BACKUP:-}"
TARGET_DSN="${CG_RESTORE_TARGET_DSN:-}"
TARGET_ALIAS="${CG_RESTORE_TARGET_ALIAS:-}"
TARGET_BUCKET="${CG_RESTORE_TARGET_BUCKET:-}"
REPORT_DIR="${CG_RESTORE_REPORT_DIR:-}"
TIMESTAMP="${CG_BACKUP_TIMESTAMP:-}"
EXECUTE=0

usage() {
  cat >&2 <<'USAGE'
用法：restore_drill.sh --pg-backup <dump|dir> --minio-backup <dir> [options]

  --pg-backup <path>       .dump 文件或其所在目录（自动取最新 postgres_*.dump）
  --minio-backup <dir>     backup_minio.sh 产出的备份目录（含 objects/ 与 objects.tsv）
  --target-dsn <dsn>       恢复目标 PG（--execute 必填）
  --target-alias <alias>   恢复目标 mc alias（--execute 必填）
  --target-bucket <name>   恢复目标 bucket（--execute 必填）
  --report <dir>           报告目录（默认与 PG 备份同目录）
  --execute                真正执行恢复；缺省为 dry-run（只打印计划，不写任何目标）
  --dry-run                显式声明 dry-run（与 --execute 互斥）
  -h, --help               显示本帮助

退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 恢复命令失败 / 5 校验失败 / 6 I/O 失败
USAGE
}

die() {
  printf '%s: %s\n' "$PROG" "$*" >&2
  exit "$EXIT_USAGE"
}

fail() {
  local code="$1"
  shift
  printf '%s: FAILED exit_code=%s %s\n' "$PROG" "$code" "$*" >&2
  exit "$code"
}

redact() {
  sed -E \
    -e 's#(://[^:/@[:space:]]+):[^@/[:space:]]*@#\1:***@#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]=)[^[:space:]]+#\1***#g'
}

require_cmd() {
  local bin="$1"
  local name="${2:-$1}"
  if ! command -v "$bin" >/dev/null 2>&1; then
    fail "$EXIT_DEP" "缺少依赖 $name（可用 CG_*_BIN 环境变量指定可执行文件）"
  fi
}

sha256_of() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$f" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$f" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$f" | awk '{print $NF}'
  else
    fail "$EXIT_DEP" "缺少 sha256sum/shasum/openssl，无法计算校验和"
  fi
}

meta_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$file"
}

parse_objects() {
  awk '
    {
      size = ""
      key = ""
      if (match($0, /"size":[0-9]+/)) {
        size = substr($0, RSTART + 7, RLENGTH - 7)
      }
      if (match($0, /"key":"[^"]*"/)) {
        key = substr($0, RSTART + 7, RLENGTH - 8)
      }
      if (key != "") {
        printf "%s\t%s\n", size, key
      }
    }'
}

compare_tsv() {
  local expected="$1" actual="$2"
  awk -F'\t' '
    NR == FNR { want[$0] = 1; next }
    { have[$0] = 1 }
    END {
      bad = 0
      for (k in want) {
        if (!(k in have)) { print "missing: " k; bad = 1 }
      }
      for (k in have) {
        if (!(k in want)) { print "unexpected: " k; bad = 1 }
      }
      exit bad
    }' "$expected" "$actual"
}

resolve_pg_backup() {
  local path="$1"
  if [ -f "$path" ]; then
    printf '%s' "$path"
    return 0
  fi
  if [ -d "$path" ]; then
    local newest
    newest="$(find "$path" -maxdepth 1 -type f -name 'postgres_*.dump' -print | LC_ALL=C sort -r | head -n 1)"
    if [ -n "$newest" ]; then
      printf '%s' "$newest"
      return 0
    fi
  fi
  return 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pg-backup) [ $# -ge 2 ] || die "--pg-backup 需要取值"; PG_BACKUP="$2"; shift 2 ;;
    --pg-backup=*) PG_BACKUP="${1#*=}"; shift ;;
    --minio-backup) [ $# -ge 2 ] || die "--minio-backup 需要取值"; MINIO_BACKUP="$2"; shift 2 ;;
    --minio-backup=*) MINIO_BACKUP="${1#*=}"; shift ;;
    --target-dsn) [ $# -ge 2 ] || die "--target-dsn 需要取值"; TARGET_DSN="$2"; shift 2 ;;
    --target-dsn=*) TARGET_DSN="${1#*=}"; shift ;;
    --target-alias) [ $# -ge 2 ] || die "--target-alias 需要取值"; TARGET_ALIAS="$2"; shift 2 ;;
    --target-alias=*) TARGET_ALIAS="${1#*=}"; shift ;;
    --target-bucket) [ $# -ge 2 ] || die "--target-bucket 需要取值"; TARGET_BUCKET="$2"; shift 2 ;;
    --target-bucket=*) TARGET_BUCKET="${1#*=}"; shift ;;
    --report) [ $# -ge 2 ] || die "--report 需要取值"; REPORT_DIR="$2"; shift 2 ;;
    --report=*) REPORT_DIR="${1#*=}"; shift ;;
    --execute) EXECUTE=1; shift ;;
    --dry-run) EXECUTE=0; shift ;;
    -h | --help) usage; exit "$EXIT_OK" ;;
    *) die "未知参数：$1" ;;
  esac
done

[ -n "$PG_BACKUP" ] || die "必须提供 --pg-backup 或 CG_RESTORE_PG_BACKUP"
[ -n "$MINIO_BACKUP" ] || die "必须提供 --minio-backup 或 CG_RESTORE_MINIO_BACKUP"
[ -e "$PG_BACKUP" ] || die "PG 备份不存在：$PG_BACKUP"
[ -d "$MINIO_BACKUP" ] || die "MinIO 备份目录不存在：$MINIO_BACKUP"

PG_FILE="$(resolve_pg_backup "$PG_BACKUP")" || die "在 $PG_BACKUP 下找不到 postgres_*.dump"
META_FILE="${PG_FILE%.dump}.meta"
OBJECTS_DIR="$MINIO_BACKUP/objects"
EXPECTED_TSV="$MINIO_BACKUP/objects.tsv"
MANIFEST="$MINIO_BACKUP/manifest.txt"

DSN_REDACTED="$(printf '%s' "$TARGET_DSN" | redact)"
TARGET_SRC="${TARGET_ALIAS:-<alias>}/${TARGET_BUCKET:-<bucket>}"
EXPECTED_ALEMBIC="unknown"
if [ -f "$META_FILE" ]; then
  EXPECTED_ALEMBIC="$(meta_value "$META_FILE" alembic_version || true)"
  [ -n "$EXPECTED_ALEMBIC" ] || EXPECTED_ALEMBIC="unknown"
fi

if [ "$EXECUTE" -ne 1 ]; then
  printf '[dry-run] component=C69 restore_drill\n'
  printf '[dry-run] pg_backup=%s（mode=%s）\n' "$PG_FILE" "$( [ "$PG_BACKUP" = "$PG_FILE" ] && printf file || printf dir )"
  printf '[dry-run] pg_meta=%s（expected alembic=%s）\n' "$META_FILE" "$EXPECTED_ALEMBIC"
  printf '[dry-run] minio_backup=%s\n' "$MINIO_BACKUP"
  printf '[dry-run] target_dsn=%s\n' "${DSN_REDACTED:-<target-dsn>}"
  printf '[dry-run] target_bucket=%s\n' "$TARGET_SRC"
  printf '[dry-run] step 1/4 %s --list %s\n' "$PG_RESTORE_BIN" "$PG_FILE"
  printf '[dry-run] step 2/4 %s --clean --if-exists --exit-on-error --no-owner --no-privileges --dbname=%s %s\n' \
    "$PG_RESTORE_BIN" "${DSN_REDACTED:-<target-dsn>}" "$PG_FILE"
  printf '[dry-run] step 3/4 %s --dbname=%s -Atc "SELECT version_num FROM alembic_version"（对比 %s）\n' \
    "$PSQL_BIN" "${DSN_REDACTED:-<target-dsn>}" "$EXPECTED_ALEMBIC"
  printf '[dry-run] step 4/4 %s mirror --overwrite %s/objects %s；并按 objects.tsv 逐条比对\n' \
    "$MC_BIN" "$MINIO_BACKUP" "$TARGET_SRC"
  printf '[dry-run] no target modified / no report written\n'
  exit "$EXIT_OK"
fi

[ -n "$TARGET_DSN" ] || die "--execute 必须提供 --target-dsn"
[ -n "$TARGET_ALIAS" ] || die "--execute 必须提供 --target-alias"
[ -n "$TARGET_BUCKET" ] || die "--execute 必须提供 --target-bucket"
[ -d "$OBJECTS_DIR" ] || die "MinIO 备份缺少 objects/ 目录：$OBJECTS_DIR"
[ -f "$EXPECTED_TSV" ] || die "MinIO 备份缺少 objects.tsv：$EXPECTED_TSV"

require_cmd "$PG_RESTORE_BIN" pg_restore
require_cmd "$PSQL_BIN" psql
require_cmd "$MC_BIN" mc

if [ -f "$META_FILE" ]; then
  EXPECTED_SHA="$(meta_value "$META_FILE" sha256 || true)"
  if [ -n "$EXPECTED_SHA" ]; then
    ACTUAL_SHA="$(sha256_of "$PG_FILE")"
    if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
      fail "$EXIT_VERIFY" "备份 sha256 不一致：meta=$EXPECTED_SHA actual=$ACTUAL_SHA"
    fi
  fi
fi

set +e
"$PG_RESTORE_BIN" --list "$PG_FILE" >/dev/null 2>&1
LIST_RC=$?
set -e
if [ "$LIST_RC" -ne 0 ]; then
  fail "$EXIT_VERIFY" "pg_restore --list 无法读取备份：$PG_FILE"
fi

set +e
"$PSQL_BIN" --dbname="$TARGET_DSN" -Atc 'SELECT 1' >/dev/null 2>&1
PING_RC=$?
set -e
if [ "$PING_RC" -ne 0 ]; then
  fail "$EXIT_CMD" "目标 PostgreSQL 不可达（退出码 $PING_RC）"
fi

set +e
"$PG_RESTORE_BIN" --clean --if-exists --exit-on-error --no-owner --no-privileges \
  --dbname="$TARGET_DSN" "$PG_FILE" 2>&1 | redact
RESTORE_RC=${PIPESTATUS[0]}
set -e
if [ "$RESTORE_RC" -ne 0 ]; then
  fail "$EXIT_CMD" "pg_restore 恢复失败（原始退出码 $RESTORE_RC）"
fi

RESTORED_ALEMBIC="$({ "$PSQL_BIN" --dbname="$TARGET_DSN" -Atc 'SELECT version_num FROM alembic_version LIMIT 1' 2>/dev/null || true; } | awk 'NF { printf "%s", $0; exit }')"
if [ -z "$RESTORED_ALEMBIC" ]; then
  fail "$EXIT_VERIFY" "恢复后 alembic_version 为空"
fi
if [ "$EXPECTED_ALEMBIC" != "unknown" ] && [ "$RESTORED_ALEMBIC" != "$EXPECTED_ALEMBIC" ]; then
  fail "$EXIT_VERIFY" "alembic head 不一致：expected=$EXPECTED_ALEMBIC restored=$RESTORED_ALEMBIC"
fi

TMP_DIR="$(dirname -- "$PG_FILE")"
ACTUAL_TSV="$TMP_DIR/.restore_drill.$$.tsv"
set +e
"$MC_BIN" mirror --overwrite "$OBJECTS_DIR" "$TARGET_ALIAS/$TARGET_BUCKET" 2>&1 | redact
MIRROR_RC=${PIPESTATUS[0]}
set -e
if [ "$MIRROR_RC" -ne 0 ]; then
  rm -f -- "$ACTUAL_TSV" || true
  fail "$EXIT_CMD" "mc mirror 恢复失败（原始退出码 $MIRROR_RC）"
fi

set +e
"$MC_BIN" ls --recursive --json "$TARGET_ALIAS/$TARGET_BUCKET" 2>/dev/null | parse_objects >"$ACTUAL_TSV"
LIST2_RC=${PIPESTATUS[0]}
set -e
if [ "$LIST2_RC" -ne 0 ]; then
  rm -f -- "$ACTUAL_TSV" || true
  fail "$EXIT_VERIFY" "mc ls 列举恢复目标失败（原始退出码 $LIST2_RC）"
fi

if ! compare_tsv "$EXPECTED_TSV" "$ACTUAL_TSV" >"$TMP_DIR/.restore_drill.diff.$$"; then
  printf '%s: 对象清单差异（前 20 行）：\n' "$PROG" >&2
  head -n 20 "$TMP_DIR/.restore_drill.diff.$$" >&2 || true
  rm -f -- "$ACTUAL_TSV" "$TMP_DIR/.restore_drill.diff.$$" || true
  fail "$EXIT_VERIFY" "MinIO 对象数量/内容与备份不一致"
fi
rm -f -- "$TMP_DIR/.restore_drill.diff.$$" || true

OBJECT_COUNT="$(wc -l <"$EXPECTED_TSV" | tr -d ' ')"
MANIFEST_COUNT=""
if [ -f "$MANIFEST" ]; then
  MANIFEST_COUNT="$(meta_value "$MANIFEST" object_count || true)"
fi
if [ -n "$MANIFEST_COUNT" ] && [ "$MANIFEST_COUNT" != "$OBJECT_COUNT" ]; then
  rm -f -- "$ACTUAL_TSV" || true
  fail "$EXIT_VERIFY" "manifest object_count=$MANIFEST_COUNT 与 objects.tsv=$OBJECT_COUNT 不一致"
fi
rm -f -- "$ACTUAL_TSV" || true

if [ -z "$REPORT_DIR" ]; then
  REPORT_DIR="$TMP_DIR"
fi
mkdir -p -- "$REPORT_DIR" || fail "$EXIT_IO" "无法创建报告目录：$REPORT_DIR"
REPORT="$REPORT_DIR/restore_drill_${TIMESTAMP:-$(date -u '+%Y%m%dT%H%M%SZ')}.log"
{
  printf 'component=C69\n'
  printf 'pg_backup=%s\n' "$PG_FILE"
  printf 'minio_backup=%s\n' "$MINIO_BACKUP"
  printf 'expected_alembic=%s\n' "$EXPECTED_ALEMBIC"
  printf 'restored_alembic=%s\n' "$RESTORED_ALEMBIC"
  printf 'object_count=%s\n' "$OBJECT_COUNT"
  printf 'target_bucket=%s\n' "$TARGET_SRC"
  printf 'exit_code=0\n'
} >"$REPORT" || fail "$EXIT_IO" "无法写报告：$REPORT"

printf '%s: OK exit_code=0 alembic=%s object_count=%s report=%s\n' \
  "$PROG" "$RESTORED_ALEMBIC" "$OBJECT_COUNT" "$REPORT"
exit "$EXIT_OK"