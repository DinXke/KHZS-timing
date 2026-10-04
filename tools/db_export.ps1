# Leest een LOKALE kopie van de SwimTime-database (read-only) en geeft per reeks de ruwe tijden als JSON.
# Wordt aangeroepen door livetiming.py; nooit op het origineel op de SwimTime-pc gebruiken.
#
# Codes in tabel Times (afgeleid uit vergelijking met de broadcast):
#   Channel E / Side R  = startsignaal
#   Channel B / Side S  = startblok (reactie)
#   Channel T / Side S  = aantikplaat (paneel); Invers N = indrukken, I = loslaten
#   Channel 2 / Side S  = peer (handtijd)
#   Channel C / Side U  = manuele keuze van de operator
#   RelayNr = lapnummer (0 = voor/bij de start), Status V = geldig, I = genegeerd
param([Parameter(Mandatory)][string]$File, [int]$SessionNumber = 0)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$cs = "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$File;ReadOnly=1;Exclusive=0;"
$c = New-Object System.Data.Odbc.OdbcConnection $cs
$c.Open()
try {
    function Q($sql) {
        $cmd = $c.CreateCommand(); $cmd.CommandText = $sql
        $da = New-Object System.Data.Odbc.OdbcDataAdapter $cmd
        $dt = New-Object System.Data.DataTable
        [void]$da.Fill($dt)
        , $dt
    }
    $sess = Q "SELECT ID, SessionNumber, MeetID FROM Sessions ORDER BY ID DESC"
    $sid = $null
    foreach ($s in $sess) { if ($SessionNumber -eq 0 -or $s.SessionNumber -eq $SessionNumber) { $sid = $s.ID; break } }
    if ($null -eq $sid) { @{ error = "sessie $SessionNumber niet gevonden" } | ConvertTo-Json -Compress; exit 0 }
    $rows = Q ("SELECT e.EventNr, h.HeatNumber, l.LaneNr, t.RelayNr, t.DayTime, t.Channel, t.Side, t.Invers, t.Status " +
               "FROM ((Times t INNER JOIN Lanes l ON t.LaneID=l.ID) INNER JOIN Heat h ON l.HeatID=h.ID) " +
               "INNER JOIN Event e ON h.EventID=e.ID WHERE e.SessionID=$sid AND t.Status='V' ORDER BY t.DayTime")
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($r in $rows) {
        $out.Add(@($r.EventNr, $r.HeatNumber, $r.LaneNr, $r.RelayNr, $r.DayTime, "$($r.Channel)", "$($r.Side)", "$($r.Invers)"))
    }
    # aflossingen: ploeg en deelnemers per baan
    $rel = Q ("SELECT e.EventNr, h.HeatNumber, l.LaneNr, rt.TeamName, rt.TeamNumber, rs.RelayPosition, c.FirstName, c.LastName, c.Club " +
              "FROM ((((Lanes l INNER JOIN Heat h ON l.HeatID=h.ID) INNER JOIN Event e ON h.EventID=e.ID) " +
              "INNER JOIN RelayTeam rt ON rt.ID=l.RelayTeamID) INNER JOIN RelaySet rs ON rs.RelayID=rt.ID) " +
              "INNER JOIN Competitors c ON c.ID=rs.CompID WHERE e.SessionID=$sid ORDER BY e.EventNr, h.HeatNumber, l.LaneNr, rs.RelayPosition")
    $relays = New-Object System.Collections.Generic.List[object]
    foreach ($r in $rel) {
        $relays.Add(@($r.EventNr, $r.HeatNumber, $r.LaneNr, "$($r.TeamName)", $r.TeamNumber, $r.RelayPosition, "$($r.FirstName)", "$($r.LastName)", "$($r.Club)"))
    }
    # programma (alle reeksen + startlijsten) van dezelfde wedstrijd, voor de oproepkamer
    $meet = ($sess | Where-Object { $_.ID -eq $sid } | Select-Object -First 1).MeetID
    $sch = Q ("SELECT s.SessionNumber, s.[Date] AS SDate, e.EventNr, e.[Order] AS EvOrder, e.SwimStyleName, e.Distance, e.RelayCount, " +
              "h.HeatNumber, h.Name AS HeatName, h.[Order] AS HOrder, h.DayTime AS HTime, h.Finished, " +
              "l.LaneNr, l.EntryTime, l.Status AS LStatus, l.Enabled, c.FirstName, c.LastName, c.Club, rt.TeamName " +
              "FROM ((((Heat h INNER JOIN Event e ON h.EventID=e.ID) INNER JOIN Sessions s ON e.SessionID=s.ID) " +
              "LEFT JOIN Lanes l ON l.HeatID=h.ID) LEFT JOIN Competitors c ON c.ID=l.CompetitorID) " +
              "LEFT JOIN RelayTeam rt ON rt.ID=l.RelayTeamID WHERE s.MeetID=$meet " +
              "ORDER BY s.[Date], s.DayTime, e.[Order], h.[Order], h.HeatNumber, l.LaneNr")
    $schedule = New-Object System.Collections.Generic.List[object]
    foreach ($r in $sch) {
        $ht = if ($r.HTime -is [DateTime]) { $r.HTime.ToString('HH:mm') } else { '' }
        $sd = if ($r.SDate -is [DateTime]) { $r.SDate.ToString('yyyy-MM-dd') } else { '' }
        $schedule.Add(@($r.SessionNumber, $sd, $r.EventNr, "$($r.SwimStyleName)", $r.Distance, $r.RelayCount, $r.HeatNumber,
                        "$($r.HeatName)", $ht, [bool]$r.Finished, $r.LaneNr, $(if ($r.EntryTime -is [DBNull]) { $null } else { $r.EntryTime }),
                        "$($r.LStatus)", "$($r.FirstName)", "$($r.LastName)", "$($r.Club)", "$($r.TeamName)"))
    }
    @{ session = $SessionNumber; sessionId = $sid; rows = $out; relays = $relays; schedule = $schedule } | ConvertTo-Json -Compress -Depth 4
} finally { $c.Close() }
