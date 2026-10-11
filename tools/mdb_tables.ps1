# Leest gekozen kolommen uit een LOKALE kopie van een Access-databank (alleen lezen) en geeft JSON terug.
# Gebruik: powershell -File mdb_tables.ps1 -File kopie.mdb -Spec "TABEL:KOL1,KOL2;TABEL2:KOL"
param([string]$File, [string]$Spec)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$c = New-Object System.Data.Odbc.OdbcConnection "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$File;ReadOnly=1;Exclusive=0;"
$c.Open()
$out = [ordered]@{}
foreach ($part in $Spec.Split(';')) {
    if (-not $part) { continue }
    $t, $cols = $part.Split(':')
    $cmd = $c.CreateCommand()
    $cmd.CommandText = "SELECT " + (($cols.Split(',') | ForEach-Object { "[$_]" }) -join ',') + " FROM [$t]"
    $rows = New-Object System.Collections.ArrayList
    try {
        $r = $cmd.ExecuteReader()
        while ($r.Read()) {
            $o = [ordered]@{}
            for ($i = 0; $i -lt $r.FieldCount; $i++) {
                $v = $r.GetValue($i)
                if ($v -is [DBNull]) { $v = $null }
                elseif ($v -is [DateTime]) { $v = $v.ToString('yyyy-MM-dd HH:mm:ss') }
                $o[$r.GetName($i)] = $v
            }
            [void]$rows.Add($o)
        }
        $r.Close()
    } catch { }
    $out[$t] = $rows
}
$c.Close()
$out | ConvertTo-Json -Depth 4 -Compress
