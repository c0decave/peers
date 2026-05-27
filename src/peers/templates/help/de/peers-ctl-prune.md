# peers-ctl prune — alte Log-Dateien löschen

## NAME
peers-ctl prune — entfernt vom Controller verwaltete Log-Dateien,
die älter als ein konfigurierbarer Schwellwert sind.

## SYNOPSIS
```
peers-ctl prune [--older-than-days N]
```

## BESCHREIBUNG
Läuft das `logs/`-Dir unter `$XDG_CONFIG_HOME/peers-ctl/` durch und
unlinkt jede Log-Datei deren mtime älter als N Tage ist. Druckt die
Anzahl gelöschter Files. Reconciliert vorher die Registry, damit die
State-Spalte stimmt.

Pro-Projekt-`.peers/log/runs.jsonl`-Files innerhalb des Projekts
werden NICHT angefasst — `prune` räumt nur den Controller-seitigen
Spill.

## OPTIONS
- `--older-than-days N` — Schwelle in Tagen; default 7. Muss positiv
  sein (Helper raised ValueError auf 0 oder negativ).

## BEISPIELE
```
# Default: alles älter als eine Woche.
peers-ctl prune

# Aggressiver: nur die letzten 24h behalten.
peers-ctl prune --older-than-days 1
```

## DATEIEN
- Entfernt: passende Files unter `$XDG_CONFIG_HOME/peers-ctl/logs/`.

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Config-Root-Override.

## SIEHE AUCH
- `peers-ctl logs --help-man`
- `peers-ctl tail --help-man`

## NOTES
- Aktive Projekt-Logs (von laufenden Loops) werden unabhängig vom
  Alter NICHT gepruned — Datei ist vom Prozess offen, Registry weiß
  davon.
- Aus cron oder systemd-timer für automatisches Housekeeping; Call
  ist sicher mehrfach hintereinander aufzurufen.
