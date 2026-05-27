# peers report — Markdown-Rollup schreiben

## NAME
peers report — schreibt `.peers/REPORT.md` mit State, recent Ticks,
Budgets, Warnings.

## SYNOPSIS
```
peers [-C <dir>] report
```

## BESCHREIBUNG
Liest `.peers/state.json` plus `.peers/log/runs.jsonl` und rendert
ein Markdown-Dokument mit:

- Iteration-Zähler, nächster-Peer, Peer-Rotations-Reihenfolge.
- HALTED-Warnung wenn `.peers/HALTED.md` da ist.
- Pro-Goal-State-Tabelle (id, state, diagnostic).
- Soft-Goal-Consensus-Tracker.
- Budget-Verbrauch (Iterations / Runtime / Tokens / USD).
- Pro-Peer-State, Consecutive-Fails, Recent-Fails, Cheating-Zähler.
- Tick-History (letzte 50 Einträge) mit Cost/Klassifikation pro Tick.
- Run-Termination-Events (`exit`-Records).
- Warnings-Historie (letzte 20).

Skipped malformed JSONL-Lines defensiv (stderr-Warnung mit Zähler),
damit ein einzelner kaputter Eintrag das Reporting nicht blockt.

## OPTIONS
Keine.

## BEISPIELE
```
peers report
peers -C ~/c0de/peers-c0de/meine-app report
```

## DATEIEN
- Liest: `.peers/state.json`, `.peers/log/runs.jsonl`,
  `.peers/HALTED.md` (optional).
- Schreibt: `.peers/REPORT.md`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers verify --help-man` — Hard-Gates re-checken.
- `peers replay --help-man` — Spezifischen Tick reinzoomen.
- `peers-ctl report --help-man` — Multi-Projekt-Controller-Report.

## NOTES
- Tick-Historie deckelt bei den letzten 50 Einträgen, damit
  `REPORT.md` lesbar bleibt. Vollhistorie: `runs.jsonl` direkt parsen.
- USD pro Tick auf 4 Nachkommastellen; Tokens sind Integer wie vom
  Billing-Layer gemeldet.
