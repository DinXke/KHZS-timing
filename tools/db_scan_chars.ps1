# Zoekt ongewone tekens in alle tekstvelden van een LOKALE kopie van de SwimTime-database (read-only).
param([Parameter(Mandatory)][string]$File)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$c = New-Object System.Data.Odbc.OdbcConnection "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$File;ReadOnly=1;Exclusive=0;"
$c.Open()
$tables = $c.GetSchema("Tables") | Where-Object { $_.TABLE_TYPE -eq 'TABLE' } | ForEach-Object { $_.TABLE_NAME }
foreach ($t in $tables) {
    if ($t -eq 'Times') { continue }
    $cmd = $c.CreateCommand(); $cmd.CommandText = "SELECT * FROM [$t]"
    try { $r = $cmd.ExecuteReader() } catch { continue }
    $row = 0
    while ($r.Read()) {
        $row++
        for ($i = 0; $i -lt $r.FieldCount; $i++) {
            if ($r.IsDBNull($i)) { continue }
            $v = $r.GetValue($i)
            if ($v -isnot [string]) { continue }
            $odd = @()
            foreach ($ch in $v.ToCharArray()) {
                $o = [int]$ch
                $normal = ($o -ge 32 -and $o -le 126) -or ($o -ge 0xC0 -and $o -le 0xFF -and $o -ne 0xD7 -and $o -ne 0xF7)
                if (-not $normal) { $odd += ('U+{0:X4}' -f $o) }
            }
            if ($odd.Count) {
                $id = try { $r['ID'] } catch { $row }
                "{0}.{1} (ID {2}): '{3}'  -> {4}" -f $t, $r.GetName($i), $id, $v, (($odd | Select-Object -Unique) -join ',')
            }
        }
    }
    $r.Close()
}
$c.Close()
