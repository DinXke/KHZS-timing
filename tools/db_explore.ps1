# Verkent een LOKALE kopie van de SwimTime-database (read-only). Nooit op het origineel gebruiken.
param([string]$File, [string[]]$Tables = @(), [int]$Rows = 5, [string]$Query = "")
$cs = "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$File;ReadOnly=1;Exclusive=0;"
$c = New-Object System.Data.Odbc.OdbcConnection $cs
$c.Open()
function Show($sql) {
    $cmd = $c.CreateCommand(); $cmd.CommandText = $sql
    $da = New-Object System.Data.Odbc.OdbcDataAdapter $cmd
    $dt = New-Object System.Data.DataTable
    [void]$da.Fill($dt)
    "cols: " + (($dt.Columns | ForEach-Object { "$($_.ColumnName):$($_.DataType.Name)" }) -join ', ')
    $dt | Format-Table -AutoSize | Out-String -Width 400
}
if ($Query) { Show $Query }
foreach ($t in $Tables) { "===== $t"; Show "SELECT TOP $Rows * FROM [$t]" }
$c.Close()
