# peers run-check — Check-Skript per Name auflösen und ausführen

## NAME
peers run-check — findet ein Check-Skript (im `.peers/checks/` des
Projekts, in einem Mode-`checks/`, oder über `mode:check`-qualifiziert)
und startet es via `python3`. Wird von `cmd:`-Zeilen in scaffolded
`goals.yaml` benutzt.

## SYNOPSIS
```
peers [-C <dir>] run-check <name>
peers [-C <dir>] run-check <mode>:<check_name>
```

## BESCHREIBUNG
Auflösungsreihenfolge für unqualifizierten `name`:

1. `<target>/.peers/checks/<name>.py` (häufigster Fall — beim
   `peers init` dorthin kopiert).
2. Ansonsten: alle discovered Modes nach `checks/<name>.py`
   durchsuchen. Genau ein Treffer → starten. Mehrere → Exit 1 mit
   `mode:name`-Vorschlagsliste zur Disambiguierung. Null → Exit 1
   mit deduplizierter Liste verfügbarer Check-Namen.

Bei einem `mode:check_name` qualifizierten Call wird nur dieser Mode
durchsucht (Fallback: `.peers/checks/<check_name>.py` für
Back-Compat).

Nach Resolve wird das Skript als `python3 <resolved-path>` gestartet,
stdout/stderr werden weitervererbt, Exit-Code durchgereicht.

Nur Top-Level-`.py`-Files im `checks/`-Dir jedes Modes zählen.
Sprach-spezifische Shell-Skripte unter `checks/lang_<lang>/` werden
über ihre eigenen `cmd:`-Strings gestartet (`bash .peers/...`),
nicht über diesen Shim.

## OPTIONS
- `name` (positional, required) — bare Check-Name oder `mode:name`.

## BEISPIELE
```
# Default-Self-Review-Check (kopiert nach .peers/checks/).
peers run-check verify_self_review

# Disambiguieren wenn derselbe Name in zwei Modes existiert.
peers run-check audit:coverage_3class

# Verfügbare anzeigen lassen (mit Fake-Namen).
peers run-check no-such-thing 2>&1 | grep available
```

## DATEIEN
- Liest: `<target>/.peers/checks/*.py` + Bundled-Mode-`checks/`.

## UMGEBUNGSVARIABLEN
- `PEERS_MODES_DIR` — extra Mode-Discovery-Dir.

## SIEHE AUCH
- `peers-ctl modes show <name>` — `checks/`-Dir eines Modes anschauen.
- `peers verify --help-man` — Goals re-checken (nicht Einzelskripte).

## NOTES
- Für `cmd:`-Zeilen in `goals.yaml`: erlaubt
  `peers -C . run-check coverage_3class` statt
  `python3 -m peers.templates.modes.audit.checks.coverage_3class`.
- Ambiguity-Fehler sind beabsichtigt — stilles Wählen einer von zwei
  gleichnamigen Checks wäre überraschend; User muss qualifizieren.
