# 把 config.env（KEY=VALUE 行）加载为当前进程的环境变量。
# start_local.ps1 与 run_once.ps1 共用：. (Join-Path $PSScriptRoot 'load_config.ps1')
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ConfigFile = Join-Path $RepoRoot 'config.env'
if (-not (Test-Path $ConfigFile)) {
    Write-Error "找不到 $ConfigFile —— 先复制 local\config.local.example 为 config.env 并填好值。"
    exit 1
}
foreach ($line in Get-Content $ConfigFile -Encoding UTF8) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    $i = $t.IndexOf('=')
    if ($i -lt 1) { continue }
    $name = $t.Substring(0, $i).Trim()
    $value = $t.Substring($i + 1).Trim()
    Set-Item -Path "Env:$name" -Value $value
}
Set-Location $RepoRoot
