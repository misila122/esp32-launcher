#Requires -Version 5.1
<#
  把 configurator\esp32_configurator.py 打包成单文件 exe。

  用法（在本目录下）：
      powershell -ExecutionPolicy Bypass -File build_exe.ps1
      powershell -ExecutionPolicy Bypass -File build_exe.ps1 -Clean      # 先清掉上次的 build/dist

  产物：
      configurator\dist\ESP32-Launcher-Configurator.exe

  说明：
    * 配置器只用 Python 标准库 + tkinter，外加 tools\ 里的 extract_icon.py / mk_icons.py。
      所以打出来很小，也不需要额外的 --add-data（那两个模块是被 import 的，PyInstaller 会一起收进去）。
    * PyInstaller 没装的话脚本会自己 pip install --user pyinstaller。
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [string]$Name = "ESP32-Launcher-Configurator"
)

$ErrorActionPreference = "Stop"
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$root  = Split-Path -Parent $here
$tools = Join-Path $root "tools"

Write-Host "== 配置器打包 ==" -ForegroundColor Cyan
Write-Host "项目根 : $root"
Write-Host "配置器 : $here"

# ---- 1. Python -------------------------------------------------------------
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "PATH 里找不到 python，先装 Python 3.8+ 再来。" }
Write-Host "Python : $($py.Source)"
& python -c "import sys; print('版本   :', sys.version.split()[0])"

# ---- 2. PyInstaller --------------------------------------------------------
& python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "没装 PyInstaller，正在安装 ..." -ForegroundColor Yellow
    & python -m pip install --user pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败（要联网）。" }
}
$piVer = (& python -m PyInstaller --version) 2>&1 | Select-Object -Last 1
Write-Host "PyInstaller : $piVer"

# ---- 3. 图标 ---------------------------------------------------------------
# 同一个 app.ico 干两件事：--icon 决定 exe 在资源管理器里长什么样，
# --add-data 把它塞进包里，程序启动时再 iconbitmap() 一次，标题栏和任务栏才是同一个图标。
# 只做前者的话，窗口左上角那只 Tk 羽毛会一直挂着。
$ico = Join-Path $here "app.ico"
Write-Host "重新生成 app.ico ..."
& python (Join-Path $here "make_icon.py") $ico
if ($LASTEXITCODE -ne 0) { throw "make_icon.py 失败。" }

# ---- 4. 清理 ---------------------------------------------------------------
if ($Clean) {
    foreach ($d in @("build", "dist", "__pycache__")) {
        $p = Join-Path $here $d
        if (Test-Path $p) { Remove-Item -Recurse -Force $p; Write-Host "清掉 $d" }
    }
}

# ---- 5. 打包 ---------------------------------------------------------------
# --paths ../tools 让分析阶段能找到 extract_icon / mk_icons；
# --hidden-import 再钉一遍，防止它们被当成"没用到"而漏掉。
$args = @(
    "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--noconfirm",
    "--name", $Name,
    "--icon", $ico,
    "--add-data", "$ico;.",
    "--distpath", (Join-Path $here "dist"),
    "--workpath", (Join-Path $here "build"),
    "--specpath", (Join-Path $here "build"),
    "--paths", $tools,
    "--hidden-import", "extract_icon",
    "--hidden-import", "mk_icons",
    (Join-Path $here "esp32_configurator.py")
)
Write-Host ""
Write-Host "运行： python $($args -join ' ')" -ForegroundColor DarkGray
& python @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

# ---- 6. 汇报 ---------------------------------------------------------------
$exe = Join-Path $here "dist\$Name.exe"
if (-not (Test-Path $exe)) { throw "没找到产物 $exe" }
$f = Get-Item $exe
$h = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host ""
Write-Host "== 打包完成 ==" -ForegroundColor Green
Write-Host ("产物 : {0}" -f $f.FullName)
Write-Host ("大小 : {0:N1} MB" -f ($f.Length / 1MB))
Write-Host ("SHA256 : {0}" -f $h)
Write-Host ""
Write-Host "这个 exe 是自包含的，拷到别的 Windows 电脑上双击就能跑（不用装 Python）。" -ForegroundColor Cyan
