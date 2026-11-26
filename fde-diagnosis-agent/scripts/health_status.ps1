# 一条命令回答三件事：数据现在健康吗、上次什么时候成功的、有没有东西在定期跑它
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\health_status.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\health_status.ps1 -MaxAgeHours 6
#
# 为什么单独做这一层：巡检已经把结果写进 eval\data_health.json 和
# eval\health_daily.log，但「去哪看」本身是个门槛。三天后回来的第一句话
# 不该是「先 cat 一下日志」，而应该是「它现在是好是坏」。
#
# 三个来源各说各的，不合并成一个绿灯：
#   data_health.json  = 最近一次「检查」的结果与时刻（手动或计划任务都算）
#   health_daily.log  = 最近一次「巡检」那一行（有 OK/FAIL 判定）
#   Get-ScheduledTask = 有没有东西会自己再跑一次
#
# 退出码：0 三条都正常；1 任一不正常（陈旧 / 红 / 任务没在跑 / 上次任务失败）

param(
    [string]$JsonPath = '',
    [string]$LogPath = '',
    [string]$TaskName = 'fde_diagnosis_data_health',
    [double]$MaxAgeHours = 24
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $JsonPath) { $JsonPath = Join-Path $root 'eval\data_health.json' }
if (-not $LogPath) { $LogPath = Join-Path $root 'eval\health_daily.log' }

$problems = @()

# ---- 1) 最近一次检查（来源 data_health.json）----
$dh = $null
$ageH = $null
$ageMin = $null
if (Test-Path -LiteralPath $JsonPath) {
    try {
        $dh = Get-Content -LiteralPath $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        $problems += "data_health.json 读不出来：$($_.Exception.Message)"
    }
} else {
    $problems += "没有 eval\data_health.json（检查从未跑过，或跑在别的机器上）"
}

if ($dh) {
    $ca = [datetime]::MinValue
    $parsed = [datetime]::TryParseExact("$($dh.checked_at)", 'yyyy-MM-dd HH:mm:ss',
        [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$ca)
    if (-not $parsed) {
        $problems += "checked_at 读不出来：'$($dh.checked_at)'"
    } else {
        # 留一份不四舍五入的分钟数：显示「几分钟前」时用 ageH（已 Round 到 0.1 小时）
        # 会算出 0，三四分钟也显示「0 分钟前」，读起来像刚跑过
        $ageMin = ((Get-Date) - $ca).TotalMinutes
        $ageH = [math]::Round($ageMin / 60, 1)
        if ($ageH -gt $MaxAgeHours) { $problems += "最近一次检查是 $ageH 小时前，超过 $MaxAgeHours 小时" }
    }
    if ($dh.status -ne 'ok') {
        $issuesText = (@($dh.issues) -join ' / ')
        $problems += "检查结果不是 ok：$($dh.status)（$issuesText）"
    }
}

# ---- 2) 最近一次巡检（来源 health_daily.log）----
# 初始化为 DateTime 而不是 $null：TryParseExact 的 out 参数要 [ref] 一个
# 已确定类型的变量，$null 会让它匹配不到函数签名（报「找不到 5 参数重载」）
$lastAt = [datetime]::MinValue
$lastVerdict = ''
$lastLine = ''
$totalRuns = 0
$failCount = 0
if (Test-Path -LiteralPath $LogPath) {
    $lines = @(Get-Content -LiteralPath $LogPath -Encoding UTF8)
    # 只认结果行，跳过失败时落的「---- 完整输出 ----」段
    $hits = @($lines | Where-Object { $_ -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (OK|FAIL|WARN)' })
    $totalRuns = $hits.Count
    $failCount = @($hits | Where-Object { $_ -match ' (FAIL|WARN) ' }).Count
    if ($totalRuns -gt 0) {
        $lastLine = $hits[-1]
        if ($lastLine -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (OK|FAIL|WARN)') {
            $lastVerdict = $Matches[2]
            # 这里必须赋值给变量：写成 [void] 前缀时 PowerShell 匹配不到这个重载
            $okAt = [datetime]::TryParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss',
                [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$lastAt)
            if (-not $okAt) { $problems += "日志里的时间戳读不出来：$($Matches[1])" }
        }
        if ($lastVerdict -ne 'OK') { $problems += "最近一次巡检是 $lastVerdict：$lastLine" }
    }
} else {
    $problems += "没有 eval\health_daily.log（巡检从未跑过）"
}

# ---- 3) 有没有东西在定期跑（来源计划任务）----
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$ti = $null
if ($task) { $ti = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue }
if (-not $task) {
    $problems += "计划任务不存在：$TaskName（现在只有手动跑才会留下记录）"
} else {
    if ($task.State -eq 'Disabled') { $problems += "计划任务被禁用：$TaskName" }
    elseif ($task.State -ne 'Ready' -and $task.State -ne 'Running') { $problems += "计划任务状态异常：$($task.State)" }
    # 267011 = 0x41303「任务从未运行过」，不是失败
    if ($ti -and $ti.LastTaskResult -ne 0 -and $ti.LastTaskResult -ne 267011) {
        $problems += "上次计划任务执行失败：LastTaskResult=$($ti.LastTaskResult)"
    }
}

# ---- 输出：三段并排，各答一个问题 ----
$stateOk = if ($dh -and $dh.status -eq 'ok') { 'OK' } elseif ($dh) { "$($dh.status)" } else { '?' }
$freshness = if ($null -eq $ageMin -and $null -eq $ageH) { '未知' } elseif (-not $ageH -or ($ageMin -lt 60)) { "$([math]::Round($ageMin)) 分钟前" } else { "$ageH 小时前" }
# data_health.json 里每一块都是对象，不是格式化好的字符串——
# 这里逐字段取，"契约 16/16" 那种写法只在 run_eval 写 meta 时才存在
$blocks = if ($dh) {
    $ct = "$($dh.contract.pass)/$($dh.contract.total)"
    # 隔离区只报「涨」：0 涨不是信息，涨了才是
    $qd = $dh.quarantine
    $qt = if (-not $qd.readable) { '读不到' } elseif ($qd.delta -gt 0) { "$($qd.rows) 条（较上次 +$($qd.delta)）" } else { "$($qd.rows) 条" }
    "            契约 $ct · 矩阵一致 $($dh.matrix.b_consistent) · 增量重复 $($dh.incremental.duplicated) · 删除 活跃 $($dh.delete.live_devices)/软删 $($dh.delete.soft_deleted) · 隔离 $qt"
} else { '' }

Write-Host ""
Write-Host ("数据健康    {0,-7} 检查于 {1}（{2}）" -f $stateOk, "$($dh.checked_at)", $freshness)
if ($blocks) { Write-Host $blocks }
if ($lastVerdict) {
    Write-Host ("巡检        {0,-7} 最近 {1}，累计 {2} 次（其中 {3} 次非 OK）" -f `
            $lastVerdict, $lastAt.ToString('yyyy-MM-dd HH:mm:ss'), $totalRuns, $failCount)
} else {
    Write-Host "巡检        ?       （没有可识别的巡检记录）"
}
if ($task) {
    $nextText = if ($ti -and $ti.NextRunTime) { $ti.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '?' }
    $lastText = if ($ti -and $ti.LastRunTime.Year -ge 2000) { $ti.LastRunTime.ToString('MM-dd HH:mm') } else { '从未运行' }
    $resultText = if ($ti) { "result $($ti.LastTaskResult)" } else { '' }
    Write-Host ("计划任务    {0,-7} Next {1} · Last {2} · {3} · 登录时运行" -f `
            $task.State, $nextText, $lastText, $resultText)
} else {
    Write-Host "计划任务    ?       未注册"
}

if ($problems.Count -eq 0) {
    Write-Host ""
    Write-Host "三条都正常" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "不正常（$($problems.Count) 条）：" -ForegroundColor Yellow
foreach ($p in $problems) { Write-Host "  [!] $p" }
Write-Host ""
Write-Host "修法提示：数据红看 eval\health_daily.log 尾部；检查太旧就跑 scripts\health_daily.ps1；没有计划任务就跑 scripts\register_health_task.ps1 -Apply"
exit 1
