<#
  把 PC 端服务装成「开机自动运行的后台服务」—— 用计划任务实现，
  不弹黑窗口、开机登录后自动起来。

  为什么需要管理员权限：
      服务要以「管理员」身份运行，否则点屏幕上某些程序起不来 ——
      清单里写了 requireAdministrator 的 exe（游戏启动器、硬件工具很常见），
      低权限进程启动它会直接报 WinError 740。
      服务本身就是管理员时，再启动这类程序不会再弹 UAC。

  用法（在 pc 目录下，需要管理员身份的 PowerShell）：
      powershell -ExecutionPolicy Bypass -File .\install.ps1            # 安装 + 立即启动
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoStart   # 只安装不启动
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -Remove    # 卸载
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -Status    # 看状态

  不是管理员身份时：加 -Elevate 会自动弹一次 UAC 提权重跑本脚本。
#>
param(
  [switch]$Remove,
  [switch]$NoStart,
  [switch]$Status,
  [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ESP32AppLauncher'
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$script   = Join-Path $here 'launcher_server.py'
$logDir   = Join-Path $here 'logs'

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Python {
  foreach ($name in 'pythonw.exe', 'python.exe') {
    $g = Get-Command $name -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
  }
  throw "找不到 python，请先装 Python 3.9+ 并加入 PATH"
}

# ---------------------------------------------------------------- Status
if ($Status) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Host "未安装（计划任务 $TaskName 不存在）" -ForegroundColor Yellow; exit 0 }
  $i = Get-ScheduledTaskInfo -TaskName $TaskName
  Write-Host "任务     : $TaskName"
  Write-Host "状态     : $($t.State)"
  Write-Host "上次运行 : $($i.LastRunTime)  结果码: $($i.LastTaskResult)"
  Write-Host "下次运行 : $($i.NextRunTime)"
  Write-Host "日志文件 : $(Join-Path $logDir 'agent.log')"
  $p = Get-Process pythonw, python -ErrorAction SilentlyContinue |
       Where-Object { $_.Path -or $true } | Select-Object -First 5 Id, ProcessName
  if ($p) { Write-Host "python 进程:"; $p | Format-Table -AutoSize | Out-String | Write-Host }
  exit 0
}

# ---------------------------------------------------------------- Remove
if ($Remove) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载计划任务 $TaskName" -ForegroundColor Green
  } else {
    Write-Host "本来就没装" -ForegroundColor Yellow
  }
  exit 0
}

# ---------------------------------------------------------------- Install
if (-not (Test-Path $script)) { throw "找不到 $script" }

# RunLevel Highest 的计划任务必须由管理员注册
if (-not (Test-Admin)) {
  if ($Elevate) {
    Write-Host "正在申请管理员权限（会弹一次 UAC）..." -ForegroundColor Yellow
    $argList = @('-ExecutionPolicy','Bypass','-NoProfile','-File', "`"$PSCommandPath`"")
    if ($NoStart) { $argList += '-NoStart' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList -Wait
    exit 0
  }
  Write-Host ""
  Write-Host "需要管理员权限。" -ForegroundColor Red
  Write-Host "服务要用管理员身份运行，否则点屏幕上某些程序起不来" -ForegroundColor Yellow
  Write-Host "（清单里写了 requireAdministrator 的程序，低权限起不来）。" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "二选一：" -ForegroundColor Yellow
  Write-Host "  A. 让它自动提权（弹一次 UAC）：" -ForegroundColor Cyan
  Write-Host "       powershell -ExecutionPolicy Bypass -File .\install.ps1 -Elevate" -ForegroundColor Cyan
  Write-Host "  B. 自己用【管理员身份】开一个 PowerShell，再执行：" -ForegroundColor Cyan
  Write-Host "       cd `"$here`"; .\install.ps1" -ForegroundColor Cyan
  Write-Host ""
  Write-Host "不想提权也行：服务照常跑，5 个程序正常；" -ForegroundColor DarkGray
  Write-Host "点这类程序时电脑上会弹一次 UAC，点「是」就能开。" -ForegroundColor DarkGray
  exit 1
}

$python = Find-Python
Write-Host "python  : $python"
Write-Host "脚本    : $script"

$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable

# RunLevel Highest 是必须的：有些 exe 的清单里写着 requireAdministrator，
# 服务自己不提权就起不动它（WinError 740）。服务已经是管理员时，
# 再启动这类程序不会弹 UAC —— 否则用户每点一次屏幕都要跑去电脑上点一次确认。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Highest

# 必须先停掉在跑的旧实例：计划任务的运行级别只在【启动时】读取，
# 光用 -Force 重新注册，已经在跑的那个进程仍然保持原来的权限
# （表现为服务日志里出现 "launched elevated via ShellExecute"，
#  也就是它自己发现 740 又走了 UAC 兜底 —— RunLevel 没生效）。
$wasRunning = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -eq 'Running'
if ($wasRunning) {
  Write-Host "停掉正在运行的旧实例（否则新的运行级别不生效）..." -ForegroundColor DarkGray
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -eq 'Running' -and
         (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force `
  -Description "ESP32 触摸屏启动器：接收 ESP32 发来的 LAUNCH 指令并打开对应程序" `
  -ErrorAction Stop | Out-Null

Write-Host "已注册计划任务 $TaskName（管理员权限，登录后自动运行）" -ForegroundColor Green

if (-not $NoStart) {
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 3
  $lvl = (Get-ScheduledTask -TaskName $TaskName).Principal.RunLevel
  Write-Host "已启动（RunLevel=$lvl）。日志：$(Join-Path $logDir 'agent.log')" -ForegroundColor Green
}
Write-Host ""
Write-Host "常用命令："
Write-Host "  看状态 : powershell -ExecutionPolicy Bypass -File .\install.ps1 -Status"
Write-Host "  停服务 : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  卸载   : powershell -ExecutionPolicy Bypass -File .\install.ps1 -Remove"
