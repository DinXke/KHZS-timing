Eenmalige systeemmigraties. Bestandsnaam: NNN-korte-naam.sh (bv. 001-voorbeeld.sh), regel 2 = omschrijving.
Ze draaien als root via apply.sh, enkel na bevestiging in het beheer, en elk maar één keer per Pi
(bijgehouden in /var/lib/khzs/migrations/). Een migratie moet veilig zijn om opnieuw te proberen als ze faalt.
