# peers tmux — tmux-Session-Wrapper für den Sessions-Driver

## NAME
peers tmux — erzeugt / killt / attached eine tmux-Session mit einem
Fenster pro Peer plus ein kachelartiges Watcher-Fenster für die
Comms-Inboxes.

## SYNOPSIS
```
peers [-C <dir>] tmux up
peers [-C <dir>] tmux down
peers [-C <dir>] tmux attach
```

## BESCHREIBUNG
Beim Sessions-Driver läuft jeder Peer als langlebiger Prozess in
seinem eigenen tmux-Window (z.B. `claude --continue` oder
`codex resume`). Die Substrate tickt sie nicht; Nachrichten fließen
über `.peers/comms/`. `peers tmux` wickelt den Session-Lifecycle ein:

- `up` erzeugt Session `peers-<basename>` mit einem Window pro Peer
  (laufendes "Continue"-Kommando), plus ein `watch`-Window mit
  gekachelten Panes — eine `peers watch <peer>` pro Receiver.
- `down` killt die Session.
- `attach` macht `tmux attach -t peers-<basename>`.

`up` verweigert, wenn eine Session mit gleichem Namen schon existiert;
erst `down`, dann neu.

## OPTIONS
Keine am Parent. Sub-Subkommandos (`up`/`down`/`attach`) haben selbst
keine.

## BEISPIELE
```
peers tmux up         # Session erzeugen
peers tmux attach     # dranhängen
peers tmux down       # abreißen
```

## DATEIEN
- Liest: `.peers/config.yaml` für Peer-Enumeration.
- Die tmux-Session ist rein in-memory; nichts unter `.peers/` wird
  geschrieben.

## UMGEBUNGSVARIABLEN
- Braucht `tmux` auf PATH.
- Respektiert tmux' eigene Env (`TMUX`, `TMUX_TMPDIR`, ...).

## SIEHE AUCH
- `peers watch --help-man` — der per-Inbox-Tailer im `watch`-Fenster.
- `peers init --driver=sessions` — wählt diesen Driver beim Init.

## NOTES
- Pro-Peer-"Continue"-Kommando fällt auf eine Login-Shell zurück
  für Tools die die Substrate nicht kennt (alles außer claude/codex).
  In dieser Pane kann man das Tool dann per Hand starten.
- Quote-sicher: Target-Pfad wird shlex-quoted bevor er in tmux-
  Kommandostrings eingebettet wird.
