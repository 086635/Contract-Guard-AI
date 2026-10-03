<#
.SYNOPSIS
    T-D8 验收栈统一启动入口：拉起 cg-c75 全栈，并在**当前终端**跟随后端实时日志。

.DESCRIPTION
    一条命令完成：① 注入验收所需的 secrets/TLS 目录；② docker compose up -d；
    ③ 轮询 https://localhost:8443/health/ready；④ 前台跟随 api/worker/worker-convert/nginx 日志。

    这样你能在同一个终端里看到每一次请求、任务派发与错误堆栈；Ctrl+C 只退出日志跟随，
    不会停掉容器（要停栈用 docker compose ... down）。

.PARAMETER NoFollow
    只启动并等待就绪，不进入日志跟随（适合在 CI/脚本里调用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev_stack_up.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev_stack_up.ps1 -NoFollow
#>
[CmdletBinding()]
param(
    [switch]$NoFollow,
    [int]$ReadyTimeoutSeconds = 90,
    [string]$LogServices = "api worker worker-convert nginx"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dockerDir = Join-Path $root "docker"
$secretsDir = Join-Path $root ".tmp_t\c75secrets"
$tlsDir = Join-Path $root ".tmp_t\c75tls"
$readyUrl = "https://localhost:8443/health/ready"

if (-not (Test-Path (Join-Path $secretsDir "postgres_password"))) {
    throw "缺少验收 secrets：$secretsDir\postgres_password"
}
if (-not (Test-Path (Join-Path $tlsDir "server.crt"))) {
    throw "缺少验收 TLS 证书：$tlsDir\server.crt"
}

$env:CG_SECRETS_DIR = $secretsDir
$env:CG_TLS_DIR = $tlsDir
$env:CG_POSTGRES_PASSWORD = (Get-Content -Raw (Join-Path $secretsDir "postgres_password")).Trim()

$composeArgs = @(
    "compose", "--env-file", "..\.env", "-p", "cg-c75",
    "-f", "docker-compose.yml",
    "-f", "docker-compose.prod.yml",
    "-f", "docker-compose.c75.yml"
)

Push-Location $dockerDir
try {
    Write-Host "[dev_stack] 启动 cg-c75 栈（api / worker / worker-convert / nginx / pg / redis / minio / qdrant / clamav）..." -ForegroundColor Cyan
    & docker @composeArgs up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败（exit=$LASTEXITCODE）" }

    Write-Host "[dev_stack] 等待 $readyUrl ..." -ForegroundColor Cyan
    $ready = $false
    for ($i = 0; $i -lt $ReadyTimeoutSeconds; $i++) {
        $code = (curl.exe -sk -o NUL -w "%{http_code}" $readyUrl) 2>$null
        if ($code -eq "200") { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if ($ready) {
        Write-Host "[dev_stack] 后端就绪（/health/ready = 200）" -ForegroundColor Green
    } else {
        Write-Warning "[dev_stack] ${ReadyTimeoutSeconds}s 内未就绪；仍继续跟随日志，请查看下方报错"
    }

    Write-Host "[dev_stack] 入口 https://localhost:8443   账号 e2e-admin / e2e-admin-pass" -ForegroundColor Green
    Write-Host "[dev_stack] rerank 精排是宿主进程：python scripts\serve_rerank.py --host 0.0.0.0 --port 8081（未起则检索自动降级为 dense，不影响功能）" -ForegroundColor DarkGray

    if (-not $NoFollow) {
        $services = $LogServices -split " "
        Write-Host "[dev_stack] 跟随实时日志（Ctrl+C 退出跟随，不停容器）" -ForegroundColor Yellow
        & docker @composeArgs logs -f --tail=80 @services
    }
}
finally {
    Pop-Location
}
