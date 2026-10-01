#!/usr/bin/env bash
# =============================================================================
# C69 · MinIO 对象镜像备份（T-D7-8）
#
# 用法：
#   scripts/backup_minio.sh --alias <mc-alias> --bucket <bucket> --out <dir> [--retain N] [--dry-run]
#
# 凭据约定（红线）：
#   · 本脚本**只接受已配置好的 mc alias 名**，不接收/不打印 access key 与 secret key
#   · 需要临时凭据时由部署方在环境里配置 MC_HOST_<alias>=https://key:secret@host，本脚本只引用别名
#
# 环境变量（CLI 优先，参数缺失时回退）：
#   CG_BACKUP_MINIO_ALIAS   mc alias 名
#   CG_BACKUP_MINIO_BUCKET  bucket 名
#   CG_BACKUP_OUT           备份根目录
#   CG_BACKUP_RETAIN        保留份数（0 或缺省 = 不清理）
#   CG_BACKUP_TIMESTAMP     覆盖 UTC 时间戳（测试/回放用）
#   CG_MINIO_MC_BIN         mc 可执行文件（默认 mc）
#
# 退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 mc mirror 失败 / 5 校验失败 / 6 I/O 失败
#
# 产出（<out>/minio_<alias>_<bucket>_<UTC 时间戳>/ 下）：
#   objects/        镜像出来的对象树（恢复演练直接 mirror 回 bucket）
#   objects.tsv     对象清单 "<size>\t<key>"（恢复后逐条比对）
#   manifest.txt    对象数量与来源（不含凭据）
#   backup.log      运维日志（含 exit_code）
# =============================================================================
set -euo pipefail

PROG="${0##*/}"
EXIT_OK=0
EXIT_USAGE=2
EXIT_DEP=3
EXIT_CMD=4
EXIT_VERIFY=5
EXIT_IO=6

MC_BIN="${CG_MINIO_MC_BIN:-mc}"

ALIAS="${CG_BACKUP_MINIO_ALIAS:-}"
BUCKET="${CG_BACKUP_MINIO_BUCKET:-}"
OUT="${CG_BACKUP_OUT:-}"
RETAIN="${CG_BACKUP_RETAIN:-0}"
TIMESTAMP="${CG_BACKUP_TIMESTAMP:-}"
DRY_RUN=0

usage() {
  cat >&2 <<'USAGE'
用法：backup_minio.sh --alias <mc-alias> --bucket <bucket> --out <dir> [--retain N] [--dry-run]

  --alias <name>   已配置的 mc alias（或 CG_BACKUP_MINIO_ALIAS）；不接收明文凭据
  --bucket <name>  bucket 名（或 CG_BACKUP_MINIO_BUCKET）
  --out <dir>      备份根目录（或 CG_BACKUP_OUT）
  --retain <n>     只保留最近 n 份备份目录（或 CG_BACKUP_RETAIN；0 = 不清理）
  --dry-run        只打印计划，不建目录、不调用 mc、不删除任何文件
  -h, --help       显示本帮助

退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 mc mirror 失败 / 5 校验失败 / 6 I/O 失败
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

timestamp() {
  if [ -n "$TIMESTAMP" ]; then
    printf '%s' "$TIMESTAMP"
  else
    date -u '+%Y%m%dT%H%M%SZ'
  fi
}

sanitize() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

# stdin：mc ls --recursive --json 输出；stdout：每行 "<size>\t<key>"
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

# 只删除 <out>/ 下、名字严格匹配 minio_<alias>_<bucket>_<UTC 时间戳> 的直接子目录
prune_dirs() {
  local keep="$1" pattern="$2"
  [ "$keep" -gt 0 ] 2>/dev/null || return 0
  local i=0 d
  while IFS= read -r d; do
    i=$((i + 1))
    [ "$i" -le "$keep" ] && continue
    [ -n "$d" ] || continue
    [ "$(dirname -- "$d")" = "$OUT" ] || continue
    local base
    base="$(basename -- "$d")"
    case "$base" in
      ${pattern}[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
      *) continue ;;
    esac
    rm -rf -- "$d" || return 1
  done < <(find "$OUT" -maxdepth 1 -mindepth 1 -type d -name "${pattern}*" -print | LC_ALL=C sort -r)
  return 0
}

plan_prune() {
  local keep="$1" pattern="$2"
  if ! [ "$keep" -gt 0 ] 2>/dev/null; then
    printf '[dry-run] retain=%s（不清理）\n' "$keep"
    return 0
  fi
  if [ ! -d "$OUT" ]; then
    printf '[dry-run] out=%s 不存在（保留策略无既有对象）\n' "$OUT"
    return 0
  fi
  local i=0 d
  while IFS= read -r d; do
    i=$((i + 1))
    [ "$i" -le "$keep" ] && continue
    [ -n "$d" ] || continue
    printf '[dry-run] would remove %s\n' "$(basename -- "$d")"
  done < <(find "$OUT" -maxdepth 1 -mindepth 1 -type d -name "${pattern}*" -print | LC_ALL=C sort -r)
}

while [ $# -gt 0 ]; do
  case "$1" in
    --alias) [ $# -ge 2 ] || die "--alias 需要取值"; ALIAS="$2"; shift 2 ;;
    --alias=*) ALIAS="${1#*=}"; shift ;;
    --bucket) [ $# -ge 2 ] || die "--bucket 需要取值"; BUCKET="$2"; shift 2 ;;
    --bucket=*) BUCKET="${1#*=}"; shift ;;
    --out) [ $# -ge 2 ] || die "--out 需要取值"; OUT="$2"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    --retain) [ $# -ge 2 ] || die "--retain 需要取值"; RETAIN="$2"; shift 2 ;;
    --retain=*) RETAIN="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit "$EXIT_OK" ;;
    *) die "未知参数：$1" ;;
  esac
done

[ -n "$ALIAS" ] || die "必须提供 --alias 或 CG_BACKUP_MINIO_ALIAS"
[ -n "$BUCKET" ] || die "必须提供 --bucket 或 CG_BACKUP_MINIO_BUCKET"
[ -n "$OUT" ] || die "必须提供 --out 或 CG_BACKUP_OUT"
case "$RETAIN" in '' | *[!0-9]*) die "--retain 必须是非负整数（当前：$RETAIN）" ;; esac

TS="$(timestamp)"
SAFE_ALIAS="$(sanitize "$ALIAS")"
SAFE_BUCKET="$(sanitize "$BUCKET")"
DIRNAME="minio_${SAFE_ALIAS}_${SAFE_BUCKET}_${TS}"
PATTERN="minio_${SAFE_ALIAS}_${SAFE_BUCKET}_"
SRC="${ALIAS}/${BUCKET}"

if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] component=C69\n'
  printf '[dry-run] source=%s（凭据只来自 mc alias / MC_HOST_<alias>，不打印）\n' "$SRC"
  printf '[dry-run] out=%s\n' "$OUT"
  printf '[dry-run] target=%s/%s\n' "$OUT" "$DIRNAME"
  printf '[dry-run] command=%s mirror --overwrite --preserve %s %s/%s/objects\n' "$MC_BIN" "$SRC" "$OUT" "$DIRNAME"
  printf '[dry-run] verify=%s ls --recursive --json %s -> %s/%s/objects.tsv\n' "$MC_BIN" "$SRC" "$OUT" "$DIRNAME"
  printf '[dry-run] retain=%s（keep newest N matching %s*）\n' "$RETAIN" "$PATTERN"
  plan_prune "$RETAIN" "$PATTERN"
  printf '[dry-run] no directory created / no file removed\n'
  exit "$EXIT_OK"
fi

require_cmd "$MC_BIN" mc

if ! mkdir -p -- "$OUT"; then
  fail "$EXIT_IO" "无法创建备份目录：$OUT"
fi
OUT="$(cd -- "$OUT" && pwd)" || fail "$EXIT_IO" "无法进入备份目录：$OUT"

TARGET="$OUT/$DIRNAME"
OBJECTS="$TARGET/objects"
MANIFEST="$TARGET/manifest.txt"
LISTING="$TARGET/objects.tsv"
LOGF="$TARGET/backup.log"
SRC_JSON="$OUT/.${DIRNAME}.src.json"
TMP_LOG="$OUT/.${DIRNAME}.tmp.log"

mkdir -p -- "$OBJECTS" || fail "$EXIT_IO" "无法创建备份目录：$OBJECTS"

set +e
"$MC_BIN" mirror --overwrite --preserve "$SRC" "$OBJECTS" >"$TMP_LOG" 2>&1
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  redact <"$TMP_LOG" >"$TARGET/backup.log" 2>/dev/null || true
  printf 'exit_code=%s\n' "$RC" >>"$TARGET/backup.log"
  rm -f -- "$TMP_LOG" "$SRC_JSON" || true
  fail "$EXIT_CMD" "mc mirror 失败（原始退出码 $RC，日志：$TARGET/backup.log）"
fi

set +e
"$MC_BIN" ls --recursive --json "$SRC" >"$SRC_JSON" 2>>"$TMP_LOG"
LIST_RC=$?
set -e
if [ "$LIST_RC" -ne 0 ]; then
  redact <"$TMP_LOG" >"$TARGET/backup.log" 2>/dev/null || true
  printf 'exit_code=%s\n' "$EXIT_VERIFY" >>"$TARGET/backup.log"
  rm -f -- "$TMP_LOG" "$SRC_JSON" || true
  fail "$EXIT_VERIFY" "mc ls 列举源 bucket 失败（原始退出码 $LIST_RC）"
fi

SRC_COUNT="$(wc -l <"$SRC_JSON" | tr -d ' ')"
BACKUP_COUNT="$(find "$OBJECTS" -type f | wc -l | tr -d ' ')"
if [ "$SRC_COUNT" != "$BACKUP_COUNT" ]; then
  redact <"$TMP_LOG" >"$TARGET/backup.log" 2>/dev/null || true
  printf 'exit_code=%s\n' "$EXIT_VERIFY" >>"$TARGET/backup.log"
  rm -f -- "$TMP_LOG" "$SRC_JSON" || true
  fail "$EXIT_VERIFY" "对象数量不一致：source=$SRC_COUNT backup=$BACKUP_COUNT"
fi

parse_objects <"$SRC_JSON" >"$LISTING"
redact <"$TMP_LOG" >>"$LOGF" 2>/dev/null || true
{
  printf 'component=C69\n'
  printf 'created_at=%s\n' "$TS"
  printf 'source=%s\n' "$SRC"
  printf 'object_count=%s\n' "$SRC_COUNT"
  printf 'exit_code=0\n'
} >"$MANIFEST"
{
  printf 'component=C69\n'
  printf 'created_at=%s\n' "$TS"
  printf 'command=%s mirror --overwrite --preserve %s %s\n' "$MC_BIN" "$SRC" "$OBJECTS"
  printf 'object_count=%s\n' "$SRC_COUNT"
  printf 'exit_code=0\n'
} >>"$LOGF"
rm -f -- "$TMP_LOG" "$SRC_JSON" || true

if ! prune_dirs "$RETAIN" "$PATTERN"; then
  printf 'exit_code=%s\n' "$EXIT_IO" >>"$LOGF"
  fail "$EXIT_IO" "保留策略清理失败（只应涉及 $OUT 下 $PATTERN* 直接子目录）"
fi

printf '%s: OK exit_code=0 target=%s object_count=%s\n' "$PROG" "$DIRNAME" "$SRC_COUNT"
exit "$EXIT_OK"