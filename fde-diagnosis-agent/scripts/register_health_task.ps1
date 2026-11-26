# 注册 / 卸载「每日数据健康巡检」计划任务。默认只打印计划，不动系统。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\register_health_task.ps1            # 预览
#   powershell -ExecutionPolicy Bypass -File scripts\register_health_task.ps1 -Apply     # 真注册
#   powershell -ExecutionPolicy Bypass -File scripts\register_health_task.ps1 -Remove    # 卸载
#
# 为什么默认不注册：注册之后它会每天连库跑四块检查。这是「有人看」的前置条件，
# 但也会每天产生一行日志（失败时更多）。注册与否是运营决定，不该由代码替人决定。
#
# 一个已知的坑：这台机器上 PostgreSQL 是服务，跑巡检时它未必起着。起了才 ok，
# 没起会记一行 FAIL —— 那不是误报，是「这个时间点环境不完整」的真实记录。

param(
    [switch]$Apply,
    [switch]$Remove,
    [string]$TaskName = 'fde_diagnosis_data_health',
    [string]$At = '09:00'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dash = Join-Path $root 'scripts\health_daily.ps1'

if (-not (Test-Path -LiteralPath $dash)) {
    Write-Host "[X] 找不到巡检脚本: $dash" -ForegroundColor Red
    exit 1
}

if ($Remove) {
    $found = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host "任务不存在：$TaskName（无需卸载）"
        exit 0
    }
    if (-not $Apply) {
        Write-Host "预览：将删除任务 $TaskName。确认要删就加 -Apply"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除 $TaskName"
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$dash`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

if (-not $Apply) {
    Write-Host "预览（没有动系统）：" -ForegroundColor Cyan
    Write-Host "  任务名    $TaskName"
    Write-Host "  触发      每天 $At（错过会补跑一次）"
    Write-Host "  执行      powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dash`""
    Write-Host "  工作目录  $root"
    Write-Host "  超时      30 分钟（四块检查实测几十秒）"
    Write-Host "  产物      eval\data_health.json（覆盖）+ eval\health_daily.log（追加）"
    Write-Host ""
    Write-Host "确认要注册就加 -Apply"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已先移除同名旧任务"
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'fde-diagnosis-agent 每日数据健康巡检：四块检查（矩阵/删除/增量/契约），只读主库与沙箱，失败只记日志不改数据' | Out-Null

Write-Host "已注册 $TaskName，每天 $At" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
