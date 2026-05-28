# peers-ctl peek — Live-Session-Events dekodieren

## NAME
peers-ctl peek — Claude-Session-jsonl eines registrierten Projekts als
lesbare Operator-Events ausgeben.

## SYNOPSIS
```
peers-ctl peek <name> [--session <id>] [--no-follow] [--last N]
```

## DESCRIPTION
Findet die neueste Claude-Session-jsonl des Projekts und dekodiert
Tool-Calls, Text-Ausgaben und Tool-Results als einzeilige Events. Der
Befehl ist read-only und beeinflusst den laufenden peers-Loop nicht.

## OPTIONS
- `name` — registrierter Projektname.
- `--session <id>` — spezifische Session-ID statt der neuesten lesen.
- `--no-follow` — vorhandene Events drucken und beenden.
- `--last N` — nur die letzten N rohen jsonl-Events vor dem Folgen zeigen.

## EXAMPLES
```
peers-ctl peek meine-app
peers-ctl peek meine-app --last 50 --no-follow
```

## DATEIEN
- Liest: `~/.claude/projects/<encoded-cwd>/*.jsonl`.
- Liest: peers-ctl Projekt-Registry.

## UMGEBUNGSVARIABLEN
- `HOME` — zum Finden der Claude-Session-jsonl-Dateien.
- `XDG_CONFIG_HOME` — Controller-Registry-Root-Override.

## SIEHE AUCH
- `peers-ctl status --help-man`
- `peers-ctl tail --help-man`
- `peers report --help-man`

## NOTES
- `peek` beobachtet nur Claude-Session-Traffic; es stoppt oder
  signalisiert keinen laufenden Peer-Prozess.
