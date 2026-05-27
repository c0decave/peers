# peers replay — Log-Einträge einer Iteration drucken

## NAME
peers replay — druckt die JSONL-Log-Einträge einer bestimmten
Iteration nochmal. Für Post-Mortem-Debugging.

## SYNOPSIS
```
peers [-C <dir>] replay <iteration>
```

## BESCHREIBUNG
Liest `.peers/log/runs.jsonl`, findet jeden Eintrag mit
`iteration == <N>`, druckt jeden als eingerücktes JSON auf stdout.
Skipped malformed Lines defensiv (am Ende eine stderr-Warnung mit
Zähler).

Jeder Tick produziert mindestens einen Log-Eintrag (Peer-Name, Tool,
Klassifikation, Dauer, Tokens, USD). Iteration 0 hat typischerweise
den Initial-Scaffold-Eintrag; Fehler + Retries können mehrere
Einträge mit gleicher `iteration` ergeben.

## OPTIONS
- `iteration` (positional, required) — die Integer-Iteration zum
  Replayen.

## BEISPIELE
```
# Alles was in Tick 7 passiert ist.
peers replay 7

# Ersten Tick stichproben.
peers -C ./meine-app replay 1
```

## DATEIEN
- Liest: `.peers/log/runs.jsonl`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers report --help-man` — Vollständiger Markdown-Rollup.
- `peers status --help-man` — Aktueller State-Snapshot.

## NOTES
- `peers replay` schaut nur in den Substrate-eigenen JSONL-Log.
  Volle pro-Peer-stdout/stderr leben unter
  `.peers/log/peers/tick-NNNN-<peer>/` — dort das Raw-Transcript.
- Wenn kein Eintrag für die angefragte Iteration existiert, Exit 1
  mit erklärender stderr-Meldung.
