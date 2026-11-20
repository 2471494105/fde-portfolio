# 录制演示：全屏 h264 mp4，固定时长，录完自动把索引放到文件头（+faststart）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\record_demo.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\record_demo.ps1 -Seconds 90 -Fps 20
#
# 说明：
#   - 全屏抓桌面（gdigrab），录的是"人眼看到的样子"，不是窗口内录。
#     所以录制开始后别再弹出聊天窗、终端、微信等无关窗口。
#   - 时长固定：三幕彩排实测 46 s，120 s 有足够余量；录长了也只是多一段静止画面。
#   - 停止方式就是等它到点，不要 Ctrl+C —— 强行结束会让 mp4 没有 moov，播不了。

param(
    [int]$Seconds = 120,
    [int]$Fps = 15,
    [int]$StartDelay = 5,
    [string]$Out = "",
    # 只录主屏（默认）。双屏机器的 gdigrab desktop 会抓 3840 宽的整块虚拟桌面，
    # 视频里一半是第二块屏，看着像事故。
    [switch]$AllDisplays,
    # 只录某个窗口（按标题子串匹配），演示最干净；找不到标题就退回主屏
    [string]$WindowTitle = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$ff = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe'
if (-not (Test-Path -LiteralPath $ff)) {
    $ff = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') `
        -Filter ffmpeg.exe -Recurse -Depth 4 -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ff -or -not (Test-Path -LiteralPath $ff)) {
    Write-Host "[X] 找不到 ffmpeg。先跑: winget install Gyan.FFmpeg" -ForegroundColor Red
    exit 1
}
$ffprobe = Join-Path (Split-Path $ff -Parent) 'ffprobe.exe'

if (-not $Out) {
    $dir = Join-Path $root 'docs\demo_recordings'
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $Out = Join-Path $dir ('demo_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.mp4')
}

# 前置检查：只警告，不替人做决定
$qmsOk = $false
try {
    $qmsOk = (Invoke-WebRequest -Uri 'http://127.0.0.1:8010/health' -UseBasicParsing -TimeoutSec 4).StatusCode -eq 200
} catch { }
if (-not $qmsOk) {
    Write-Host '[!] mock QMS 8010 不可达：第三幕建单会失败，录下来也不能用' -ForegroundColor Yellow
}

$orderN = $null
try {
    $env:PGPASSWORD = 'postgres'
    $orderN = (& 'E:\POSTGRESQL\bin\psql.exe' -h localhost -p 5432 -U postgres -d fde_rag `
        -t -A -c 'select count(*) from work_orders' 2>$null)
} catch { }
if ($orderN -and $orderN.Trim() -ne '1') {
    Write-Host "[!] work_orders 现在 $($orderN.Trim()) 张；演示前应重导 data\seed\002_data.sql 回到 1 张" -ForegroundColor Yellow
}

Write-Host ''
Write-Host "录屏输出: $Out" -ForegroundColor Cyan
Write-Host "时长 $Seconds s / $Fps fps，$StartDelay s 后开录 —— 现在切到演示窗口" -ForegroundColor Yellow
Start-Sleep -Seconds $StartDelay

# 输入源与取景：优先窗口录制，其次主屏，最后才是整块虚拟桌面
# 取景：gdigrab 的 desktop 会抓整块虚拟桌面（这台机器是 3840×1080），
# 而 -offset_x/-video_size 的原点语义和 System.Windows.Forms 报的坐标对不上
# （实测会报 "Capture area ... extends outside window area"）。
# 稳的做法：整块抓下来，再用 crop 按"主屏在虚拟桌面里的位置"裁出来。
$srcIn = 'desktop'
$frameNote = ''
$vf = ''
if ($WindowTitle) {
    $srcIn = "title=$WindowTitle"
    $frameNote = "窗口录制（标题含 `"$WindowTitle`"）"
} elseif ($AllDisplays) {
    $frameNote = '全部显示器（虚拟桌面）'
} else {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -Namespace Native -Name Metrics -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern int GetSystemMetrics(int nIndex);
"@
    $virtualX = [Native.Metrics]::GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    $virtualY = [Native.Metrics]::GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    $vw = [Native.Metrics]::GetSystemMetrics(78)
    $vh = [Native.Metrics]::GetSystemMetrics(79)
    $scr = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $cropX = $scr.X - $virtualX
    $cropY = $scr.Y - $virtualY
    if ($cropX -eq 0 -and $cropY -eq 0 -and $scr.Width -eq $vw -and $scr.Height -eq $vh) {
        $frameNote = "主屏（正好是整块虚拟桌面 $($vw)x$($vh)）"
    } else {
        $vf = 'crop=' + $scr.Width + ':' + $scr.Height + ':' + $cropX + ':' + $cropY
        $frameNote = "主屏 $($scr.Width)x$($scr.Height)，从虚拟桌面 $($vw)x$($vh) 裁起于 ($cropX,$cropY)"
    }
}
Write-Host "取景: $frameNote"

$vfArgs = @()
if ($vf) { $vfArgs = @('-vf', $vf) }

& $ff -y -f gdigrab -framerate $Fps -i $srcIn -t $Seconds @vfArgs `
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart $Out 2> "$Out.log"
if (-not (Test-Path -LiteralPath $Out)) {
    Write-Host '[X] 没生成文件' -ForegroundColor Red
    exit 1
}
$bytes = (Get-Item -LiteralPath $Out).Length
Write-Host ("[ok] {0}  {1:N1} MB" -f $Out, ($bytes / 1MB)) -ForegroundColor Green
if (Test-Path -LiteralPath $ffprobe) {
    $info = & $ffprobe -v error -select_streams v:0 -show_entries stream=width,height,duration `
        -show_entries format=duration,size -of default=noprint_wrappers=1 $Out 2>&1
    $info | Where-Object { $_ } | ForEach-Object { Write-Host ('    ' + $_) }
}