# 开机自启包装：带日志 + 重试 + 服务输出落盘
# 由 Startup 快捷方式（或计划任务）以 -WindowStyle Hidden 调用。
$ErrorActionPreference = "SilentlyContinue"

$root  = Split-Path -Parent $PSScriptRoot
$app   = Split-Path -Parent $root
$launcher = Join-Path $app "启动星引.bat"
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "autostart.log"
$outLog = Join-Path $logDir "serve.out.log"
$errLog = Join-Path $logDir "serve.err.log"

function Note([string]$msg) {
  Add-Content -Path $log -Value ("[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "] " + $msg) -Encoding UTF8
}

function ServerUp() {
  try {
    $c = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction Stop | Select-Object -First 1
    return [bool]$c
  } catch { return $false }
}

Note ("自启尝试开始（launcher: " + $launcher + "）")
if (-not (Test-Path $launcher)) { Note "找不到启动器，放弃"; exit 1 }
if (ServerUp) { Note "服务已在运行，跳过"; exit 0 }

for ($try = 1; $try -le 3; $try++) {
  Note ("启动第 " + $try + " 次")
  try {
    Start-Process -FilePath $launcher -ArgumentList "--no-browser" -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
  } catch {
    Note ("启动进程失败: " + $_.Exception.Message)
  }
  for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 3
    if (ServerUp) { Note ("服务已就绪（第 " + $try + " 次启动）"); exit 0 }
  }
  Note ("第 " + $try + " 次启动后 60s 仍未监听 8787（详见 serve.out.log / serve.err.log）")
}
Note "三次启动均失败：请手动双击启动器并查看控制台输出"
exit 1
