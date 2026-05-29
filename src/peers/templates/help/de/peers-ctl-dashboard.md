# peers-ctl dashboard — Multi-Projekt-Rollup

## NAME
peers-ctl dashboard — read-only Multi-Projekt-Tabelle mit State,
Tick-Count, offenen Gates (hard / soft), Blocking-Bug-Count,
Container-Name und Last-Tick-Timestamp.

## SYNOPSIS
```
peers-ctl dashboard [--live] [--refresh-s SEKUNDEN] [--frames N]
                    [--project NAME]
```

Für Multi-Projekt-Operator:innen ist die **Streaming-Ansicht**
(`--live`) im Alltag meist nützlicher: sie zeichnet die Tabelle alle
`--refresh-s` Sekunden neu und blendet zusätzlich die Spalten `ALERT`
und `EVENT` ein, die der einmalige Snapshot nicht hat. Der
flagless-Default ist ein einmaliger Snapshot, sinnvoll für Skripte
und schnelle Checks.

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
  Zusätzlich erscheinen `ALERT` und `EVENT` als Spalten. Die
  Streaming-Ansicht ist das, was die meisten Operator:innen für
  laufende Observability wollen.
- `--refresh-s SEKUNDEN` — Refresh-Intervall für `--live` (Default:
  `2.0`). Muss größer als null sein.
- `--frames N` — nur zusammen mit `--live` gültig. Rendert `N`
  Frames und beendet sich (Default: bis Ctrl-C). Praktisch für
  headless-Smoke-Tests und CI: `peers-ctl dashboard --live --frames 1`
  rendert genau einen Frame und exited mit 0.
- `--project NAME` — Single-Projekt-Drilldown mit jüngsten Runs und
  Bug-Reports anzeigen.

## BEISPIELE
```
# Streaming-Ansicht aller Projekte — der entdeckbare Default für
# Day-to-Day-Observability (Ctrl-C zum Beenden).
peers-ctl dashboard --live
peers-ctl dashboard --live --refresh-s 1

# Einmaliger Snapshot — gut zum Pipen in andere Tools.
peers-ctl dashboard

# Non-interaktiver Smoke-Test: ein Frame rendern und exiten.
peers-ctl dashboard --live --frames 1

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
