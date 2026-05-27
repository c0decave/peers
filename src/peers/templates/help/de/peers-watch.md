# peers watch — Comms-Inboxes für einen Receiver tailen

## NAME
peers watch — pollt `.peers/comms/<from>-to-<receiver>/` auf neue
Nachrichten und streamt sie auf stdout. Für den Sessions-Driver, wo
jeder Peer eine langlebige tmux-Session ist.

## SYNOPSIS
```
peers [-C <dir>] watch <receiver> [--poll-s SEKUNDEN]
```

## BESCHREIBUNG
Beim Sessions-Driver läuft jeder Peer als langlebiger Prozess (z.B.
`claude --continue`); die Substrate started ihn nicht pro Tick. Die
Inter-Peer-Nachrichten landen als Dateien unter
`.peers/comms/<from>-to-<receiver>/`. `peers watch` ist ein kleiner
Filesystem-Poller, der neue Dateien beim Auftauchen ausgibt — eine
tmux-Pane mit `peers watch claude` zeigt also alles was codex (oder
andere Peers) schicken.

Dateien matchen `[0-9][0-9][0-9][0-9]-*.md` (vierstelliges
Sequenz-Präfix). Bereits gesehene Dateien werden für die Prozess-
Lebensdauer gemerkt.

Läuft endlos; Ctrl-C bricht ab (Exit 0).

## OPTIONS
- `receiver` (positional, required) — Peer-Name, dessen Inbox getailt
  wird (z.B. `claude` für `codex-to-claude/`-Traffic).
- `--poll-s SEKUNDEN` — Poll-Intervall; default 1.0 s.

## BEISPIELE
```
# In einer tmux-Pane:
peers watch claude

# In einer anderen:
peers watch codex --poll-s 0.5
```

## DATEIEN
- Liest: `.peers/comms/*-to-<receiver>/[0-9][0-9][0-9][0-9]-*.md`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers tmux up --help-man` — fertiges Sessions-Driver-Layout mit
  vorverdrahteten Watcher-Panes.
- `peers run --help-man` — Orchestrator-Driver-Alternative (ohne
  langlebige Sessions).

## NOTES
- Jede Nachricht wird im Output bei 64 KiB gekappt; längere Files
  bekommen einen `--- truncated ---`-Marker.
- Receiver-Name wird validiert; ungültige Namen (Pfad-Separatoren
  etc.) werden mit Exit 2 abgelehnt.
