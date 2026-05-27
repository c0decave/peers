# peers-ctl start — Projekt-Loop starten

## NAME
peers-ctl start — spawnt `peers run` für ein registriertes Projekt im
Hintergrund, optional im `peers:dev`-Container.

## SYNOPSIS
```
peers-ctl start <name> [--max-ticks N] [--max-usd USD] [--container]
```

## BESCHREIBUNG
Löst `<name>` gegen die Registry auf, startet `peers run` (mit den
Flags unten durchgereicht) detached vom Controller, schreibt PID +
Log-Pfad in die Registry und gibt eine Ein-Zeilen-Bestätigung aus.
Die Loop läuft bis sie selbst terminiert (Goals durch, Budget aus,
max-ticks, halted) oder bis `peers-ctl stop <name>`.

`--container` fährt die Loop im `peers:dev`-Podman-Container, mit
Mounts:
- Projekt-Dir auf `/work`,
- `~/.claude/` für claude-CLI-Auth,
- `~/.codex/` für codex-CLI-Auth.

Container-Pfad ist bevorzugt wenn der Host die Peer-CLIs
(claude/codex) nicht installiert hat, das Image aber schon.

stdout/stderr der Loop wird in ein Controller-eigenes Logfile unter
dem Config-Dir geteed; tailen mit `peers-ctl tail <name>`.

## OPTIONS
- `name` (positional, required) — registrierter Projektname.
- `--max-ticks N` — Tick-Cap für diesen Run.
- `--max-usd USD` — `budget.max_usd` für diesen Run überschreiben.
- `--container` — im `peers:dev`-Container statt auf dem Host fahren.

## BEISPIELE
```
# Host-Run mit Sicherheitsnetzen.
peers-ctl start meine-app --max-ticks 50 --max-usd 5

# Container-Run mit Podman-Host-Network.
PEERS_CTL_PODMAN_NETWORK=host \
  peers-ctl start meine-app --container --max-ticks 100 --max-usd 50

# Offen (kein Iterations-Cap) — nur wenn man sicher ist.
peers-ctl start meine-app --max-usd 20
```

## DATEIEN
- Liest: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.
- Schreibt: Registry-Eintrag (PID, Log-Pfad) + öffnet Log-File unter
  `logs/`-Subdir des Config-Dirs.

## UMGEBUNGSVARIABLEN
- `PEERS_CTL_PODMAN_NETWORK` — Podman-Netzwerkmodus (`host`, `none`, ...).
- `PODMAN_CMD` — `podman`-Pfad-Override.
- Peer-spezifische Env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) wird
  durchgereicht.

## SIEHE AUCH
- `peers-ctl stop --help-man` — sauberes Shutdown.
- `peers-ctl tail --help-man` — Log follow.
- `peers run --help-man` — was `start` tatsächlich aufruft.

## NOTES
- Image-Major-Version muss zu Host-`peers`-Package-Major passen,
  sonst Container-Start refuses mit `make build`-Hint.
- Für OAuth-Subscription-Peers (claude / codex CLI) ist
  `--max-usd` informativ; expliziter Hard-Cap-Mode muss in
  `config.yaml` stehen.
