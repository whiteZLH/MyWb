# start.ps1 —— MyWb 一键启动（Windows / PowerShell）
#
# 首次运行会在脚本所在目录自动生成 .env（随机管理密钥与客户端 Key），随后启动服务。
# 所有数据（SQLite、登录态）都落在当前脚本目录；目录锚点即“当前目录”。
#
# 配置方式（可选）：
#   - 想自定义端口 / 开关，直接编辑生成的 .env（参考 .env.example）。
#   - 已存在 .env 时脚本不会覆盖，只负责启动。
#
# 用法：在项目根目录执行  .\start.ps1

$ErrorActionPreference = "Stop"

# 以脚本所在目录作为运行目录（.env 与数据的锚点 = 当前目录）
Set-Location -Path $PSScriptRoot

# 生成 URL 安全的随机串（等价 Python 的 secrets.token_urlsafe）
function New-Token([int]$Length) {
    $bytes = New-Object byte[] ([int][math]::Ceiling($Length * 3 / 4) + 4)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $text = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    return $text.Substring(0, $Length)
}

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    $adminKey = New-Token 36
    $clientKey = "sk-wb-" + (New-Token 32)
    @(
        "ADMIN_KEY=$adminKey"
        "CODEBUDDY2OPENAI_KEY=$clientKey"
        "PORT=8787"
        "ADMIN_SECURE_COOKIE=0"
        "CODEBUDDY_AUTH_DIR=$PSScriptRoot"
        "MANAGEMENT_DATA_DIR=$PSScriptRoot"
    ) | Set-Content -LiteralPath $envFile -Encoding utf8

    Write-Host "已生成 .env：$envFile" -ForegroundColor Green
    Write-Host "  管理密钥   ADMIN_KEY             : $adminKey"
    Write-Host "  客户端 Key CODEBUDDY2OPENAI_KEY : $clientKey"
    Write-Host "  （请妥善保管，删除 .env 后会重新生成）"
    Write-Host ""
}

# 存在虚拟环境则激活；否则使用系统 Python（需已安装依赖）
if (Test-Path -LiteralPath ".\.venv\Scripts\Activate.ps1") {
    Write-Host "激活虚拟环境 .venv ..." -ForegroundColor Cyan
    & ".\.venv\Scripts\Activate.ps1"
}

Write-Host "启动管理后台：http://127.0.0.1:8787/admin/ （Ctrl+C 停止）" -ForegroundColor Green
python -m admin.server
