# peers-ctl modes — verfügbare Audit-Modes inspizieren

## NAME
peers-ctl modes — listet oder zeigt Audit-/Security-/Thorough-Modes,
die `peers init --modes=...` (und `peers-ctl new --modes=...`)
aufnehmen können.

## SYNOPSIS
```
peers-ctl modes list
peers-ctl modes show <name>
```

## BESCHREIBUNG
`modes list` fährt `peers.modes.discover()` und druckt eine Tabelle:

```
NAME              VER    SOURCE    DESCRIPTION
audit             v3     builtin   Default audit gates (...)
security          v1     builtin   OWASP-getriebener Security-Review
my-custom-mode    v0     user      ...
```

`modes show <name>` dumpt `mode.yaml` des Modes, dessen `goals.yaml`
und ein Listing seines `checks/`-Dirs. Damit lässt sich
sanity-checken was `--modes=<name>` in `.peers/` reinkopieren würde.

User-Modes (unter `$PEERS_MODES_DIR` oder
`~/.config/peers/modes/<name>/`) shadowen Built-in-Modes nach Namen
— die SOURCE-Spalte sagt welche Kopie gewinnt.

## OPTIONS
- `list` — keine Optionen.
- `show <name>` — required positional: Mode-Name.

## BEISPIELE
```
peers-ctl modes list
peers-ctl modes show audit
peers-ctl modes show security | less
```

## DATEIEN
- Liest: Bundled-Modes unter `peers/templates/modes/`,
  `$PEERS_MODES_DIR/<name>/`, `~/.config/peers/modes/<name>/`.

## UMGEBUNGSVARIABLEN
- `PEERS_MODES_DIR` — extra Discovery-Dir (zusätzlich zu Bundled +
  `~/.config/peers/modes/`).

## SIEHE AUCH
- `peers init --help-man` — `--modes` nutzt diese Discovery.
- `peers-ctl new --help-man` — gleicher `--modes`-Flag.

## NOTES
- `show` liest aus dem Package ODER `$PEERS_MODES_DIR`, beide
  trusted, also plain `read_text()` statt hardened-no-symlink.
- Unbekannte Mode-Namen → Exit 1 mit verfügbarer Namensliste.
