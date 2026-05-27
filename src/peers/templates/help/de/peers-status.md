# peers status — Iteration, Lock und Goal-Status drucken

## NAME
peers status — druckt einen kurzen menschen-lesbaren Snapshot des
Loop-Zustands. Read-only, nichts wird gestartet.

## SYNOPSIS
```
peers [-C <dir>] status
```

## BESCHREIBUNG
Liest `.peers/state.json` (v1 → v2 in-memory migriert) und druckt:

- Aktuelle Iteration und welcher Peer als nächstes dran ist.
- Lock-Status: held (PID), present-but-stale, present-but-empty.
- HALTED-Flag und Dirty-Worktree-Warnung (falls zutrifft).
- Budget-Rollup: Iterationen, Runtime, Tokens, USD.
- Pro-Goal-Status (pass / fail / pending) + Diagnostic.
- Pro-Peer-Status + Consecutive-Fail-Zähler + letzte Klassifikation.
- Letzte Warnings (bis zu 5) und Run-Log-Eintrag-Gesamtzahl.

Nur lesend — startet keine Peers, ändert keinen State.

## OPTIONS
Keine.

## BEISPIELE
```
peers status
peers -C ~/c0de/peers-c0de/meine-app status
```

## DATEIEN
- Liest: `.peers/state.json`, `.peers/run.lock`, `.peers/HALTED.md`,
  `.peers/log/runs.jsonl`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers report --help-man` — detaillierter Markdown-Rollup.
- `peers info --help-man` — Konfig-Dump (Peers/Goals/Budget).
- `peers-ctl status --help-man` — Multi-Projekt-Sicht.

## NOTES
- Steht `.peers/run.lock` als „stale" da (Datei da, flock nicht
  gehalten), ist ein vorheriger Run ungesund gestorben; neu starten
  ist sicher.
- v1-State-Dateien werden in-memory migriert; die Datei auf Platte
  wird beim nächsten Persist vom Orchestrator neu geschrieben.
