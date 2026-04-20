#!/usr/bin/env powershell
# Lite API - PostgreSQL 安装与配置脚本
# 使用方法: .\setup_postgres.ps1

$ErrorActionPreference = "Stop"

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Lite API - PostgreSQL 安装向导" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# ---- 1. 检查 PostgreSQL ----
$pgInstalled = $false
try {
    $pgVersion = & psql --version 2>$null
    $pgInstalled = $true
    Write-Host "[OK] PostgreSQL 已安装: $pgVersion" -ForegroundColor Green
} catch {
    Write-Host "[!] PostgreSQL 未安装" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "请手动安装 PostgreSQL:" -ForegroundColor White
    Write-Host "  1. 访问 https://www.postgresql.org/download/windows/" -ForegroundColor White
    Write-Host "  2. 下载并安装 PostgreSQL 17" -ForegroundColor White
    Write-Host "  3. 安装时记住设置的超级用户密码" -ForegroundColor White
    Write-Host "  4. 安装完成后重新运行此脚本" -ForegroundColor White
    Write-Host ""
    Write-Host "或者使用 winget 安装:" -ForegroundColor White
    Write-Host "  winget install -e --id PostgreSQL.PostgreSQL" -ForegroundColor White
    Write-Host ""
    Read-Host "安装完成后按回车继续"
}

# ---- 2. 配置数据库 ----
$pgUser = "liteapi"
$pgPass = "liteapi123"
$pgDb = "liteapi"
$pgHost = "localhost"
$pgPort = "5432"

Write-Host ""
Write-Host "---- 数据库配置 ----" -ForegroundColor Cyan
$superPass = Read-Host "请输入 PostgreSQL 超级用户(postgres)密码"

# 创建用户和数据库
Write-Host "[*] 创建数据库用户和数据库..." -ForegroundColor Yellow
$env:PGPASSWORD = $superPass

try {
    # 创建用户
    & psql -h $pgHost -p $pgPort -U postgres -c "CREATE USER $pgUser WITH PASSWORD '$pgPass';" 2>$null
    Write-Host "[OK] 用户 $pgUser 已创建" -ForegroundColor Green
} catch {
    Write-Host "[~] 用户 $pgUser 可能已存在" -ForegroundColor Yellow
}

try {
    # 创建数据库
    & psql -h $pgHost -p $pgPort -U postgres -c "CREATE DATABASE $pgDb OWNER $pgUser;"
    Write-Host "[OK] 数据库 $pgDb 已创建" -ForegroundColor Green
} catch {
    Write-Host "[~] 数据库 $pgDb 可能已存在" -ForegroundColor Yellow
}

try {
    # 授权
    & psql -h $pgHost -p $pgPort -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE $pgDb TO $pgUser;"
    Write-Host "[OK] 权限已授予" -ForegroundColor Green
} catch {
    Write-Host "[!] 授权失败" -ForegroundColor Red
}

Remove-Item Env:\PGPASSWORD

# ---- 3. 更新 config.yaml ----
Write-Host ""
Write-Host "[*] 更新 config.yaml..." -ForegroundColor Yellow
$yamlPath = Join-Path $PSScriptRoot "config.yaml"
$yaml = Get-Content $yamlPath -Raw -Encoding UTF8
$yaml = $yaml -replace '# url: "postgresql\+asyncpg://.*"', "url: ""postgresql+asyncpg://${pgUser}:${pgPass}@${pgHost}:${pgPort}/${pgDb}"""
$yaml = $yaml -replace 'url: "sqlite\+aiosqlite:///./lite_api.db"', "# url: ""sqlite+aiosqlite:///./lite_api.db"""
Set-Content $yamlPath $yaml -Encoding UTF8
Write-Host "[OK] config.yaml 已更新" -ForegroundColor Green

# ---- 4. 验证连接 ----
Write-Host ""
Write-Host "[*] 验证数据库连接..." -ForegroundColor Yellow
$env:PGPASSWORD = $pgPass
try {
    $result = & psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -c "SELECT 1;" 2>&1
    Write-Host "[OK] 数据库连接成功!" -ForegroundColor Green
} catch {
    Write-Host "[!] 连接失败: $_" -ForegroundColor Red
}
Remove-Item Env:\PGPASSWORD

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  配置完成! 运行 start.bat 启动服务" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
