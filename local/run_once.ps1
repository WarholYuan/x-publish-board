# 给 Windows 任务计划调用的一次性任务：加载 config.env 后跑一次渲染或 FxTwitter 兜底。
#   powershell -ExecutionPolicy Bypass -File local\run_once.ps1 -Task render
#   powershell -ExecutionPolicy Bypass -File local\run_once.ps1 -Task fx
param([Parameter(Mandatory = $true)][ValidateSet('render', 'fx')][string]$Task)
. (Join-Path $PSScriptRoot 'load_config.ps1')
$Py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
if ($Task -eq 'render') {
    & $Py 'board\render_board.py' >> 'data\logs\render.log'
} else {
    & $Py 'collect\fx_poll.py' >> 'data\logs\fx_poll.log'
}
exit $LASTEXITCODE
