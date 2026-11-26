param([string]$Psql = "E:\POSTGRESQL\bin\psql.exe", [string]$HostAddr = "127.0.0.1", [string]$Port = "5432")

# 一条命令把三步跑完，重复跑不会报错：
#   1) 三个账号（集群级）
#   2) 建库 fde_client（不存在才建）
#   3) schema 骨架与授权
# 用法：powershell -File scripts\bootstrap.ps1

$ErrorActionPreference = "Stop"
$sql = Join-Path (Split-Path -Parent $PSScriptRoot) "sql"
$env:PGPASSWORD = "postgres"

function Run-Sql([string]$db, [string]$file) {
    $label = Split-Path -Leaf $file
    & $Psql -h $HostAddr -p $Port -U postgres -d $db -v ON_ERROR_STOP=1 -f $file
    if ($LASTEXITCODE -ne 0) { throw ('{0} failed, exit={1}' -f $label, $LASTEXITCODE) }
    Write-Output ('[ok] {0} -> {1}' -f $label, $db)
}

Run-Sql "postgres" (Join-Path $sql "001_roles.sql")

$exists = & $Psql -h $HostAddr -p $Port -U postgres -d postgres -t -A -c "select 1 from pg_database where datname='fde_client'"
if ($LASTEXITCODE -ne 0) { throw 'query pg_database failed' }
# 没有这行输出时 psql 返回空，$exists 会是 $null，直接 .Trim() 会炸
$exists = ($exists | Out-String).Trim()
if ($exists -ne '1') {
    Run-Sql "postgres" (Join-Path $sql "002_database.sql")
} else {
    Write-Output '[skip] 002_database.sql: fde_client exists'
}

Run-Sql "fde_client" (Join-Path $sql "003_schema.sql")
Write-Output '[done] fde_client ready'