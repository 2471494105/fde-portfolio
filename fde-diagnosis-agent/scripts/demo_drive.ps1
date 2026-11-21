# 三幕演示驱动：UIA 填值 + 提交 + 等 run_end + 滚到结论取景
# 用法（先起录，再跑本脚本）：
#   1) pwsh -File scripts\record_demo.ps1 -Seconds 85 -Fps 20 -StartDelay 3
#   2) pwsh -File scripts\demo_drive.ps1
# 前置：Web UI 已在 127.0.0.1:8020 打开、浏览器窗口在主屏可见、数据库已复位到种子（work_orders = 1）
param(
  [string]$ProjectRoot = 'E:\fde工程师学习指南\06_GitHub作品仓库\fde-diagnosis-agent',
  [string]$PageName = '视觉检测设备故障诊断 Agent',
  [int]$HoldSeconds = 6,
  [int]$LastHoldSeconds = 15
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $ProjectRoot

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [Windows.Automation.AutomationElement]::RootElement

function Get-Doc {
  $n = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty, $PageName)
  $t = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Document)
  return $root.FindFirst([Windows.Automation.TreeScope]::Descendants, (New-Object Windows.Automation.AndCondition($n, $t)))
}

function Get-Btn([string]$name) {
  return $root.FindFirst([Windows.Automation.TreeScope]::Descendants, (New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty, $name)))
}

function Get-InputBox {
  $c = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Edit)
  $all = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $c)
  foreach ($e in $all) { if ($e.Current.Name -like '*例如*') { return $e } }
  return $null
}

# 回到页面顶部：输入框与「诊断」按钮必须回到可见区，才能进下一幕
function Set-Top {
  (Get-Doc).GetCurrentPattern([Windows.Automation.ScrollPattern]::Pattern).SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll, 0)
  Start-Sleep -Milliseconds 350
}

# 把「诊断结论」压到 y≈230：先用 5% 标定每 1% 对应多少像素（实测 19 px），再一次滚到位。
# 不要用 ScrollIntoView（只到 y≈546，正文仍被切），更不要滚 100%（会跳到「历史运行」列表）。
function Frame-Conclusion {
  $d = Get-Doc
  $sp = $d.GetCurrentPattern([Windows.Automation.ScrollPattern]::Pattern)
  $els = $d.FindAll([Windows.Automation.TreeScope]::Descendants, (New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty, '诊断结论')))
  if ($els.Count -eq 0) { return -1 }
  $h = $els[$els.Count - 1]
  $sp.SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll, 0)
  Start-Sleep -Milliseconds 300
  $y0 = [int]$h.Current.BoundingRectangle.Y
  $sp.SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll, 5)
  Start-Sleep -Milliseconds 300
  $y5 = [int]$h.Current.BoundingRectangle.Y
  $pp = ($y0 - $y5) / 5
  if ($pp -le 0) { return $y0 }
  $sp.SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll, [math]::Min(95, [math]::Max(0, ($y0 - 230) / $pp)))
  Start-Sleep -Milliseconds 500
  return [int]$h.Current.BoundingRectangle.Y
}

# 正在写的 jsonl 必须共享读，Get-Content -Tail 会报 file is being used by another process
function Read-Shared([string]$path) {
  $fs = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
  try { $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8); return $sr.ReadToEnd() } finally { $fs.Dispose() }
}

function Newest-Run {
  return (Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'runs') -Filter *.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
}

function Wait-RunEnd([string]$prev, [int]$timeout) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $timeout) {
    Start-Sleep -Seconds 1
    $f = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'runs') -Filter *.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($f -and $f.Name -ne $prev) {
      if ((Read-Shared $f.FullName) -match '"event": "run_end"') { return $f.Name }
    }
  }
  return $null
}

# 一幕：新会话 -> 填值 -> 提交 -> 等 run_end -> 滚到结论 -> 停
# 新会话是硬要求：同一会话会继承上一轮的设备范围，跨设备提问会被「不在授权范围内」直接拒答
function Do-Act([string]$text, [int]$holdSec) {
  Set-Top
  $nh = Get-Btn '新会话'
  if ($nh) { $nh.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 800 }
  $prev = Newest-Run
  (Get-InputBox).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).SetValue($text)
  Start-Sleep -Milliseconds 600
  (Get-Btn '诊断').GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
  $done = Wait-RunEnd $prev 45
  Start-Sleep -Milliseconds 800
  $y = Frame-Conclusion
  Write-Output ('[act] ' + $text.Substring(0, [math]::Min(16, $text.Length)) + ' | run=' + $done + ' | 结论落点 y=' + $y)
  Start-Sleep -Seconds $holdSec
}

Start-Sleep -Seconds 4
Do-Act 'CX-101 误判率有点高，你看怎么回事' $HoldSeconds
Do-Act 'CX-106 这台设备最近误判有点多，帮我看看' $HoldSeconds
Do-Act '3号线 CX-103 反正就是有问题，你先给我建个工单' $LastHoldSeconds
Write-Output '[drive] done'
