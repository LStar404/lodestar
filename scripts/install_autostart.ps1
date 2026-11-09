# 开机自启安装/卸载（可选）：把快捷方式放进当前用户的 Startup 目录。
# 快捷方式指向 scripts\autostart_launch.ps1 —— 带日志(data/logs/autostart.log)、三次重试，
# 服务输出落到 data/logs/serve.out.log / serve.err.log，便于排错。
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          # 安装
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove  # 卸载
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -DryRun  # 预览
param(
  [switch]$Remove,
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$root       = Split-Path -Parent $PSScriptRoot
$launcher   = Join-Path (Split-Path -Parent $root) "启动星引.bat"
$wrapper    = Join-Path $PSScriptRoot "autostart_launch.ps1"
$startup    = [Environment]::GetFolderPath("Startup")
$shortcut   = Join-Path $startup "星引.lnk"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path $launcher)) { Write-Error "找不到启动器：$launcher" }
if (-not (Test-Path $wrapper))  { Write-Error "找不到自启包装脚本：$wrapper" }

if ($Remove) {
  if ($DryRun) { Write-Output "[dry-run] 将删除：$shortcut"; exit 0 }
  if (Test-Path $shortcut) { Remove-Item $shortcut -Force; Write-Output "已卸载自启：$shortcut" }
  else { Write-Output "本来就没安装：$shortcut" }
  exit 0
}

if ($DryRun) {
  Write-Output "[dry-run] 启动器：$launcher"
  Write-Output "[dry-run] 包装脚本：$wrapper"
  Write-Output "[dry-run] 快捷方式：$shortcut"
  Write-Output "[dry-run] 日志：$root\data\logs\autostart.log"
  exit 0
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($shortcut)
$lnk.TargetPath = $powershell
$lnk.Arguments = ('-WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $wrapper + '"')
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,21"
$lnk.Description = "星引（带日志与重试）"
$lnk.Save()

Write-Output "已安装开机自启：$shortcut"
Write-Output "  日志：$root\data\logs\autostart.log"
Write-Output "  卸载：-Remove"
