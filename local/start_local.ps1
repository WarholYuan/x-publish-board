# 无服务器模式一键启动（Windows）：在本机同时拉起状态服务、回采接收端
# 与本地网关，并先渲染一次发布板。关掉这个窗口（或 Ctrl+C）后到任务管理器
# 结束残留的 python 进程即可全停；常驻请改用任务计划 + run_once.ps1（见 docs/no-server.md）。
#
# 前置：仓库根目录有 config.env（本地版从 local\config.local.example 复制改），
#       lark-cli 已在本机以你的飞书账号登录，Python 3.10+ 可用（命令名 python）。
# 启动后打开：http://127.0.0.1:8799/x/
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'load_config.ps1')

$Py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }

$logDir = Join-Path $RepoRoot 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
foreach ($p in @($env:XBOARD_OUT_DIR,
                 (Split-Path $env:XBOARD_TOKEN_FILE -Parent),
                 $env:XCOLLECT_STATE_DIR,
                 (Split-Path $env:XCOLLECT_TOKEN_FILE -Parent),
                 (Split-Path $env:EXTENSION_ZIP_OUT -Parent))) {
    if ($p) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}

Write-Host '== 首次渲染发布板 =='
& $Py 'board\render_board.py'
if ($LASTEXITCODE -ne 0) {
    Write-Warning '渲染失败（多半是 lark-cli 未登录或飞书配置没填对）。服务仍会启动，修好后手动再跑一次 render_board.py。'
}

$procs = @()
try {
    $procs += Start-Process $Py -ArgumentList 'board\state_server.py' -WorkingDirectory $RepoRoot -PassThru -WindowStyle Minimized -RedirectStandardOutput (Join-Path $logDir 'state.log') -RedirectStandardError (Join-Path $logDir 'state.err.log')
    $procs += Start-Process $Py -ArgumentList 'collect\ingest_server.py' -WorkingDirectory $RepoRoot -PassThru -WindowStyle Minimized -RedirectStandardOutput (Join-Path $logDir 'ingest.log') -RedirectStandardError (Join-Path $logDir 'ingest.err.log')
    $procs += Start-Process $Py -ArgumentList 'local\gateway.py' -WorkingDirectory $RepoRoot -PassThru -WindowStyle Minimized -RedirectStandardOutput (Join-Path $logDir 'gateway.log') -RedirectStandardError (Join-Path $logDir 'gateway.err.log')
    Start-Sleep -Seconds 1
    $port = if ($env:XLOCAL_PORT) { $env:XLOCAL_PORT } else { '8799' }
    Write-Host ''
    Write-Host '已启动（日志在 data\logs\）：'
    Write-Host "  发布板（本机）： http://127.0.0.1:$port/x/"
    Write-Host "  回采令牌文件：   $env:XCOLLECT_TOKEN_FILE（填进扩展 popup）"
    Write-Host '  想要手机访问 / https 地址：另开一个终端跑  tailscale funnel --bg ' $port
    Write-Host '  停：在这里按回车。'
    Read-Host | Out-Null
} finally {
    foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
}
