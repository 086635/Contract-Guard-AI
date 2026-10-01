#!/usr/bin/env bash
# =============================================================================
# C69 · PostgreSQL 备份（T-D7-8）
#
# 用法：
#   scripts/backup_postgres.sh --dsn <postgres-dsn> --out <dir> [--retain N] [--dry-run]
#
# 环境变量（CLI 优先，参数缺失时回退）：
#   CG_BACKUP_PG_DSN       PG 连接串（含口令；绝不打印/落盘明文）
#   CG_BACKUP_OUT          备份根目录
#   CG_BACKUP_RETAIN       保留份数（0 或缺省 = 不清理）
#   CG_BACKUP_TIMESTAMP    覆盖 UTC 时间戳（测试/回放用）
#   CG_PG_DUMP_BIN         pg_dump 可执行文件（默认 pg_dump）
#   CG_PG_RESTORE_BIN      pg_restore 可执行文件（默认 pg_restore）
#   CG_PSQL_BIN            psql 可执行文件（默认 psql；缺失时 alembic 版本记 unknown）
#
# 退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 pg_dump 失败 / 5 校验失败 / 6 I/O 失败
#
# 产出（<out>/ 下，命名族严格受保留策略约束）：
#   postgres_<db>_<YYYYMMDDThhmmssZ>.dump   custom-format 备份（pg_restore --list 可校验）
#   postgres_<db>_<...>.meta                无 secret 的元数据（sha256 / alembic 版本）
#   postgres_<db>_<...>.sha256              dump 的 sha256
#   postgres_<db>_<...>.log                 运维日志（含 exit_code）
# =============================================================================
set -euo pipefail

PROG="${0##*/}"
EXIT_OK=0
EXIT_USAGE=2
EXIT_DEP=3
EXIT_CMD=4
EXIT_VERIFY=5
EXIT_IO=6

PG_DUMP_BIN="${CG_PG_DUMP_BIN:-pg_dump}"
PG_RESTORE_BIN="${CG_PG_RESTORE_BIN:-pg_restore}"
PSQL_BIN="${CG_PSQL_BIN:-psql}"

DSN="${CG_BACKUP_PG_DSN:-}"
OUT="${CG_BACKUP_OUT:-}"
RETAIN="${CG_BACKUP_RETAIN:-0}"
TIMESTAMP="${CG_BACKUP_TIMESTAMP:-}"
DRY_RUN=0

usage() {
  cat >&2 <<'USAGE'
用法：backup_postgres.sh --dsn <postgres-dsn> --out <dir> [--retain N] [--dry-run]

  --dsn <dsn>       PG 连接串（或 CG_BACKUP_PG_DSN）；支持 URL 与 key=value 形式
  --out <dir>       备份根目录（或 CG_BACKUP_OUT）
  --retain <n>      只保留最近 n 份（或 CG_BACKUP_RETAIN；0 = 不清理）
  --dry-run         只打印计划，不建目录、不调用 pg_dump、不删除任何文件
  -h, --help        显示本帮助

退出码：0 成功 / 2 参数错误 / 3 依赖缺失 / 4 pg_dump 失败 / 5 校验失败 / 6 I/O 失败
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

# stdin -> stdout：抹掉 URL 口令与 key=value 口令形式，保证任何日志都不落明文
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

timestamp() {
  if [ -n "$TIMESTAMP" ]; then
    printf '%s' "$TIMESTAMP"
  else
    date -u '+%Y%m%dT%H%M%SZ'
  fi
}

db_name_from_dsn() {
  local dsn="$1" candidate=""
  case "$dsn" in
    *dbname=*)
      candidate="${dsn#*dbname=}"
      candidate="${candidate%%[[:space:]]*}"
      ;;
  esac
  if [ -z "$candidate" ]; then
    case "$dsn" in
      *://*)
        local hostpart="${dsn#*://}"
        local pathpart="${hostpart#*/}"
        if [ "$pathpart" != "$hostpart" ]; then
          candidate="${pathpart%%\?*}"
        fi
        ;;
    esac
  fi
  if [ -z "$candidate" ]; then
    candidate="default"
  fi
  printf '%s' "$candidate" | tr -c 'A-Za-z0-9._-' '_'
}

# 只清理由本脚本创建、且严格匹配 postgres_<db>_<UTC 时间戳>.dump 的命名族
prune_files() {
  local keep="$1" pattern="$2" unit="$3"
  [ "$keep" -gt 0 ] 2>/dev/null || return 0
  local i=0 f
  while IFS= read -r f; do
    i=$((i + 1))
    [ "$i" -le "$keep" ] && continue
    [ -n "$f" ] || continue
    [ "$(dirname -- "$f")" = "$OUT" ] || continue
    local base
    base="$(basename -- "$f" ".$unit")"
    case "$base" in
      ${pattern}[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
      *) continue ;;
    esac
    rm -f -- "$f" || return 1
    rm -f -- "$OUT/${base}.meta" "$OUT/${base}.sha256" "$OUT/${base}.log" || return 1
  done < <(find "$OUT" -maxdepth 1 -type f -name "${pattern}*.${unit}" -print | LC_ALL=C sort -r)
  return 0
}

plan_prune() {
  local keep="$1" pattern="$2" unit="$3"
  if ! [ "$keep" -gt 0 ] 2>/dev/null; then
    printf '[dry-run] retain=%s（不清理）\n' "$keep"
    return 0
  fi
  if [ ! -d "$OUT" ]; then
    printf '[dry-run] out=%s 不存在（保留策略无既有对象）\n' "$OUT"
    return 0
  fi
  local i=0 f
  while IFS= read -r f; do
    i=$((i + 1))
    [ "$i" -le "$keep" ] && continue
    [ -n "$f" ] || continue
    printf '[dry-run] would remove %s\n' "$(basename -- "$f")"
  done < <(find "$OUT" -maxdepth 1 -type f -name "${pattern}*.${unit}" -print | LC_ALL=C sort -r)
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dsn) [ $# -ge 2 ] || die "--dsn 需要取值"; DSN="$2"; shift 2 ;;
    --dsn=*) DSN="${1#*=}"; shift ;;
    --out) [ $# -ge 2 ] || die "--out 需要取值"; OUT="$2"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    --retain) [ $# -ge 2 ] || die "--retain 需要取值"; RETAIN="$2"; shift 2 ;;
    --retain=*) RETAIN="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit "$EXIT_OK" ;;
    *) die "未知参数：$1" ;;
  esac
done

[ -n "$DSN" ] || die "必须提供 --dsn 或 CG_BACKUP_PG_DSN"
[ -n "$OUT" ] || die "必须提供 --out 或 CG_BACKUP_OUT"
case "$RETAIN" in '' | *[!0-9]*) die "--retain 必须是非负整数（当前：$RETAIN）" ;; esac

TS="$(timestamp)"
DB="$(db_name_from_dsn "$DSN")"
NAME="postgres_${DB}_${TS}"
PATTERN="postgres_${DB}_"
DSN_REDACTED="$(printf '%s' "$DSN" | redact)"

if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] component=C69\n'
  printf '[dry-run] out=%s\n' "$OUT"
  printf '[dry-run] dsn=%s\n' "$DSN_REDACTED"
  printf '[dry-run] artifact=%s.dump\n' "$NAME"
  printf '[dry-run] command=%s --format=custom --no-owner --no-privileges --dbname=%s --file=%s.dump\n' \
    "$PG_DUMP_BIN" "$DSN_REDACTED" "$NAME"
  printf '[dry-run] verify=%s --list %s.dump；sha256 -> %s.sha256\n' "$PG_RESTORE_BIN" "$NAME" "$NAME"
  printf '[dry-run] retain=%s（keep newest N matching %s*.dump）\n' "$RETAIN" "$PATTERN"
  plan_prune "$RETAIN" "$PATTERN" dump
  printf '[dry-run] no directory created / no file removed\n'
  exit "$EXIT_OK"
fi

require_cmd "$PG_DUMP_BIN" pg_dump
require_cmd "$PG_RESTORE_BIN" pg_restore

if ! mkdir -p -- "$OUT"; then
  fail "$EXIT_IO" "无法创建备份目录：$OUT"
fi
OUT="$(cd -- "$OUT" && pwd)" || fail "$EXIT_IO" "无法进入备份目录：$OUT"

DUMP="$OUT/${NAME}.dump"
META="$OUT/${NAME}.meta"
SHAFILE="$OUT/${NAME}.sha256"
LOGF="$OUT/${NAME}.log"
TMP_LOG="$OUT/.${NAME}.tmp.log"

: >"$LOGF" || fail "$EXIT_IO" "无法写入日志：$LOGF"

ALEMBIC="unknown"
if command -v "$PSQL_BIN" >/dev/null 2>&1; then
  ALEMBIC="$({ "$PSQL_BIN" --dbname="$DSN" -Atc 'SELECT version_num FROM alembic_version LIMIT 1' 2>/dev/null || true; } | awk 'NF { printf "%s", $0; exit }')"
  [ -n "$ALEMBIC" ] || ALEMBIC="unknown"
fi

set +e
"$PG_DUMP_BIN" --format=custom --no-owner --no-privileges --dbname="$DSN" --file="$DUMP" >"$TMP_LOG" 2>&1
RC=$?
set -e

redact <"$TMP_LOG" >>"$LOGF" 2>/dev/null || true
rm -f -- "$TMP_LOG" || true

if [ "$RC" -ne 0 ]; then
  rm -f -- "$DUMP" || true
  printf 'exit_code=%s\n' "$RC" >>"$LOGF"
  fail "$EXIT_CMD" "pg_dump 失败（原始退出码 $RC，日志：$LOGF）"
fi

if [ ! -s "$DUMP" ]; then
  rm -f -- "$DUMP" || true
  printf 'exit_code=%s\n' "$EXIT_VERIFY" >>"$LOGF"
  fail "$EXIT_VERIFY" "pg_dump 未产出非空文件：$DUMP"
fi

set +e
"$PG_RESTORE_BIN" --list "$DUMP" >/dev/null 2>>"$LOGF"
VERIFY_RC=$?
set -e
if [ "$VERIFY_RC" -ne 0 ]; then
  printf 'exit_code=%s\n' "$EXIT_VERIFY" >>"$LOGF"
  fail "$EXIT_VERIFY" "备份无法通过 pg_restore --list 校验：$DUMP"
fi

SHA="$(sha256_of "$DUMP")"
SIZE="$(wc -c <"$DUMP" | tr -d ' ')"
printf '%s  %s.dump\n' "$SHA" "$NAME" >"$SHAFILE"
{
  printf 'component=C69\n'
  printf 'created_at=%s\n' "$TS"
  printf 'artifact=%s.dump\n' "$NAME"
  printf 'size_bytes=%s\n' "$SIZE"
  printf 'sha256=%s\n' "$SHA"
  printf 'alembic_version=%s\n' "$ALEMBIC"
  printf 'source=%s\n' "$DSN_REDACTED"
} >"$META"
{
  printf 'component=C69\n'
  printf 'created_at=%s\n' "$TS"
  printf 'command=%s --format=custom --no-owner --no-privileges --dbname=%s --file=%s.dump\n' \
    "$PG_DUMP_BIN" "$DSN_REDACTED" "$NAME"
  printf 'artifact=%s.dump\n' "$NAME"
  printf 'size_bytes=%s\n' "$SIZE"
  printf 'sha256=%s\n' "$SHA"
  printf 'alembic_version=%s\n' "$ALEMBIC"
  printf 'exit_code=0\n'
} >>"$LOGF"

if ! prune_files "$RETAIN" "$PATTERN" dump; then
  printf 'exit_code=%s\n' "$EXIT_IO" >>"$LOGF"
  fail "$EXIT_IO" "保留策略清理失败（只应涉及 $OUT 下 $PATTERN*.dump 命名族）"
fi

printf '%s: OK exit_code=0 artifact=%s.dump size_bytes=%s\n' "$PROG" "$NAME" "$SIZE"
exit "$EXIT_OK"