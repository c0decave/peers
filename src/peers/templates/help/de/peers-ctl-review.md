# peers-ctl review — letzten Handoff-Self-Review anzeigen

## NAME
peers-ctl review — druckt den Body des jüngsten Commits eines
Projekts dessen Subject `Peer-Status: handoff` ist. Zum Inspizieren
des Self-Reviews am Ende eines Turns.

## SYNOPSIS
```
peers-ctl review <name>
```

## BESCHREIBUNG
Löst `<name>` gegen die Registry auf, läuft dann
`git -C <pfad> log --grep='^Peer-Status: handoff$' -n 1 --format=...`
für SHA, Subject und Body des jüngsten Handoff-Commits. Der Self-
Review-Abschnitt darin ist was die Soft-Goal-`self-review-on-handoff`
erwartet.

Nützlich für Peer-Review-Interaktionen: ein Mensch inspiziert den
letzten Handoff bevor er die nächste Phase freigibt.

## OPTIONS
- `name` (positional, required) — registrierter Projektname.

## BEISPIELE
```
peers-ctl review meine-app

# Mit less pagen.
peers-ctl review meine-app | less
```

## DATEIEN
- Liest: Registry, git-Log von `<projekt>`.

## UMGEBUNGSVARIABLEN
- `GIT_PAGER` etc. — wird vom darunterliegenden `git log`-Aufruf
  respektiert (Output wird capturet und verbatim gedruckt, Paging
  bleibt dem User überlassen).

## SIEHE AUCH
- `peers run-check verify_self_review` — der Substrate-eigene Check.
- `peers report --help-man` — breiter pro-Projekt-Rollup.

## NOTES
- Wenn noch kein `Peer-Status: handoff`-Commit existiert → Exit 1
  mit „no handoff commit found"-Meldung.
- Das grep-Pattern ist anchored — nur `Peer-Status: handoff`
  (exakt) matched; andere Varianten (z.B. `Peer-Status: deferred`)
  ignoriert.
