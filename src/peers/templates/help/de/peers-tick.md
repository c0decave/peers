# peers tick — genau EINEN Tick laufen lassen und beenden

## NAME
peers tick — exekutiert einen einzigen Tick der Peer-Loop. Gedacht
für Hook-Driver, wo der Stop-Hook jedes Peers den nächsten Tick
anstößt.

## SYNOPSIS
```
peers [-C <dir>] tick [--dry-run] [--after <peer-name>]
```

## BESCHREIBUNG
Funktional `peers run --max-ticks 1`: Config + Goals laden,
nächsten Peer via `state.turn_index` wählen, unter Health-Guard
starten, nach Commit Hard-Gates evaluieren, `state.json` updaten,
beenden. Exit-Code spiegelt die Stop-Reason-Klasse (0 für
`complete`/`max_ticks`, sonst nicht-null).

Das ist der Einsprung den claudes `Stop`-Hook und codex'
`on_stop`-Hook bekommen, wenn man `peers init --driver=hooks` wählt:
jedes Peer-Turn-Ende triggert den nächsten Tick via `peers tick`.

## OPTIONS
- `--dry-run` — am Tick-Ende den Peer-Commit reverten.
- `--after <peer-name>` — informativer Tag, welcher Peer gerade
  fertig wurde. Der nächste Tick folgt trotzdem `turn_index`; der
  Flag dient nur Log/Debug-Klarheit in Hook-Chains.

## BEISPIELE
```
# Manueller Single-Tick.
peers tick

# Hook-Einsprung nachdem claude einen Turn beendet hat.
peers -C /pfad/zum/projekt tick --after claude

# Trockenlauf-Probe.
peers tick --dry-run
```

## DATEIEN
Wie bei `peers run`. Nutzt `.peers/run.lock`, damit gleichzeitige
Hooks nicht parallel ticken.

## UMGEBUNGSVARIABLEN
Wie bei `peers run`.

## SIEHE AUCH
- `peers run --help-man`
- `peers init --help-man` (Abschnitt `--driver=hooks`)
- `.peers/hooks/` — generierte Snippets mit Install-Hinweisen.

## NOTES
- `--after` wird absichtlich NICHT als Name-Match erzwungen; feuert
  der Hook für den „falschen" Peer, folgt der nächste Tick trotzdem
  `turn_index`. Verirrte Hooks sind laut aber sicher.
