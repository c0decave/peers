# peers-ctl report — Markdown-Controller-Report schreiben

## NAME
peers-ctl report — rendert einen controller-seitigen Markdown-Report
über eines oder alle registrierten Projekte.

## SYNOPSIS
```
peers-ctl report [<name>]
```

## BESCHREIBUNG
Läuft die registrierten Projekte durch (gefiltert auf `<name>` falls
gegeben), sammelt pro Projekt Rollup-Daten (State, Ticks, Blocking-
Bug-Count, Last-Tick-Timestamp, README-Status, Controller-Log-Pfad)
und schreibt das Ergebnis unter `$XDG_CONFIG_HOME/peers-ctl/`:

- `REPORT.md` für die All-Projekte-Sicht.
- `REPORT-<name>.md` für einen Single-Projekt-Lauf.

Exit 0 bei sauberem Render, 1 wenn pro-Projekt-Warnings entstanden
sind (z.B. README fehlt / unsafe Symlink, Controller-Log unsafe).

## OPTIONS
- `name` (positional, optional) — auf ein Projekt einschränken.

## BEISPIELE
```
# Alle Projekte.
peers-ctl report

# Nur eins.
peers-ctl report meine-app
```

## DATEIEN
- Schreibt: `$XDG_CONFIG_HOME/peers-ctl/REPORT.md` (oder
  `REPORT-<name>.md`).
- Liest: Registry, jedes Projekt-`README.md`, `.peers/log/runs.jsonl`,
  `.peers/goals.yaml`.

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Config-Root-Override.

## SIEHE AUCH
- `peers report --help-man` — pro-Projekt-Report.
- `peers-ctl dashboard --help-man` — Terminal-Tabellen-View.

## NOTES
- `peers-ctl report` für Handoff-Dokumente; `peers-ctl dashboard`
  für interaktive Checks.
- Unbekannter `<name>` → Exit 1 mit erklärender stderr-Meldung.
