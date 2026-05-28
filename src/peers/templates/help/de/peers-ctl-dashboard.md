# peers-ctl dashboard — Multi-Projekt-Rollup

## NAME
peers-ctl dashboard — read-only Multi-Projekt-Tabelle mit State,
Tick-Count, offenen Gates (hard / soft), Blocking-Bug-Count,
Container-Name und Last-Tick-Timestamp.

## SYNOPSIS
```
peers-ctl dashboard [--live] [--refresh-s SEKUNDEN] [--project NAME]
```

## BESCHREIBUNG
Reconciliert die Registry, läuft dann jedes Projekt durch und
produziert eine Zeile:

```
NAME      STATE    TICKS  HARD_OPEN  SOFT_OPEN  BLOCKING  CONTAINER  LAST
meine-app running  47     2          1          3         peers-...  2026-...
alteres   idle     12     0          0          0         -          2026-...
```

- `TICKS` — aus `.peers/log/runs.jsonl` (alle Nicht-`exit`-Events).
- `HARD_OPEN` / `SOFT_OPEN` — gezählt via `peers.goals.load_goals`
  + pro-Goal-Status. Kaputtes YAML → `?`; missing → `-`.
- `BLOCKING` — ruft `peers.bug_hunt.summarize` für den open-blocking-
  Count.
- `CONTAINER` — Podman-Container-Name wenn bekannt (aus Registry-
  `notes`-Feld geparst).
- `LAST` — jüngster `runs.jsonl`-Timestamp.
- In `--live` zeigt `ALERT` `CRASHED`, `UNKNOWN`, `HALTED`,
  `BUDGET`, `DEGRADED` oder `WARN`; `EVENT` zeigt das neueste
  dekodierte Claude-Session-Event, falls vorhanden.

Spalten werden auto-sized für Terminal-freundliche Breite.

Mit `--project NAME` wechselt das Dashboard vom Multi-Projekt-Rollup
in einen Single-Projekt-Drilldown. Der Drilldown zeigt die Projektzeile,
die jüngsten `runs.jsonl`-Einträge und Bug-Report-Details. Zusammen mit
`--live` wird auch diese Ansicht kontinuierlich neu gezeichnet.

## OPTIONS
- `--live` — Dashboard kontinuierlich neu zeichnen bis Ctrl-C.
- `--refresh-s SEKUNDEN` — Refresh-Intervall für `--live` (Default:
  `2.0`). Muss größer als null sein.
- `--project NAME` — Single-Projekt-Drilldown mit jüngsten Runs und
  Bug-Reports anzeigen.

## BEISPIELE
```
peers-ctl dashboard

# Eingebaute Live-Ansicht.
peers-ctl dashboard --live --refresh-s 1

# Single-Projekt-Drilldown.
peers-ctl dashboard --project meine-app
peers-ctl dashboard --live --project meine-app
```

## DATEIEN
- Liest: Registry, jedes Projekt-`.peers/log/runs.jsonl`,
  `.peers/goals.yaml`, `.peers/state.json`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers-ctl list --help-man` — minimale Drei-Spalten-Form.
- `peers-ctl status --help-man` — Single-Projekt-Deep-View.
- `peers-ctl peek --help-man` — dekodierte Live-Claude-Session-Events.
- `peers-ctl report --help-man` — Markdown-Rollup mit Controller-Log-
  Pfaden.

## NOTES
- Dashboard-Call ist read-only. Auch bei kaputtem Projekt-YAML
  rendert die Zeile mit `?`-Platzhaltern, statt den ganzen Call zu
  versemmeln.
- Bug-Hunt-Summary-Fehler degradieren still auf `0`, damit das
  Dashboard resilient bleibt.
