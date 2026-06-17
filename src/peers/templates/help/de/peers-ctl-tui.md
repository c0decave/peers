# peers-ctl tui — Live-Cockpit auf dem Host

## NAME
peers-ctl tui — ein dunkles, zustandsgefärbtes "Mission-Control"-Terminal-UI
für eine peers-Flotte: Projekte starten, den Agenten bei der Arbeit zusehen,
lesen, was sie sagen und wie sie sich gegenseitig prüfen, sowie Gates /
Steps / Tasks, Bugs, Diffs, Budget und Konsens/Attestierung sehen — plus ein
vorausschauender Blick auf die agentic-os-Autonomie-Schicht.

## SYNOPSIS
```
pip install -e .[tui]      # einmalig: das optionale TUI-Extra installieren
peers-ctl tui
```

## BESCHREIBUNG
Ein nur-lesendes Master-Detail-Cockpit über den dateibasierten Signalen,
die eine Flotte ohnehin schreibt (`projects.yaml`, Per-Run-Zustand,
git-Trailer/Attestierung, `bugs.jsonl`, `runs.jsonl`, das Spine-Ledger).

**Optionales Extra.** Das TUI ist ein Textual-UI hinter dem optionalen
`[tui]`-Extra (`pip install -e .[tui]` zieht Textual + textual-window) —
der Kern bleibt `pyyaml`-only. Ohne das Extra gibt `peers-ctl tui` einen
freundlichen Installationshinweis aus und beendet sich sauber, ohne Absturz.

**Nur lesend; handelt nur über das Substrat.** Das Cockpit *liest* nur die
Signale. Jede *Aktion* (start/stop/resume, ack-block, amend, neues Projekt
starten) ruft die bestehenden `peers-ctl`-Verben auf, damit die Guards und
Hash-Ketten des Substrats maßgeblich bleiben — das TUI implementiert keine
Schreib-Logik neu und schreibt nie in `.peers/`.

**Layout.** Eine Fleet-Seitenleiste plus verschiebbare / vergrößerbare /
ein-/ausblendbare und herauslösbare Fenster: Peers, Gates (mit einem
History-Scrubber, der durch vergangene Ticks blättert), Tasks/Steps,
Live-Stream, Tick-Verlauf, Budget, Bugs, Konsens/Attestierung (mit
Fälschungs-Badge), Log und Diff — plus die vorausschauenden Autonomie-Fenster
(Autonomie-Ledger, Spine-Gates, Propagations-DAG, Autonomie-Feed,
Eskalations-Banner), die einen ehrlichen Leerzustand zeigen, bis das Spine
an einen operator-startbaren Modus angebunden ist.

**Ehrliche Neu-Ableitung.** CONVERGED- / Gate- / Integritäts-Urteile werden
stets aus dem Substrat NEU ABGELEITET und trauen nie dem
agent-beschreibbaren gespeicherten `independence`-Flag.

**Start-Assistent + Eingriffe.** Ein doctor-geprüfter, off-thread laufender
Assistent legt Projekte an und startet sie. Eingriffs-Dialoge zeigen das
exakte Verb und führen es aus; vertrags-berührende Operationen (amend,
ack-block) verlangen Tippen-zum-Bestätigen.

## OPTIONS
Keine (das Cockpit wird ohne Argumente gestartet). `--help-man` zeigt diese
Seite.

## BEISPIELE
```
pip install -e .[tui]
peers-ctl tui
# Mit [ und ] durch vergangene Gate-Ticks blättern; ? öffnet die Hilfe.
```

## DATEIEN
- Liest (pro Projekt): `projects.yaml`, Per-Run-Zustand, `runs.jsonl`
  (trägt jetzt einen Per-Tick-`gates`-Snapshot), `bugs.jsonl`,
  git-Trailer / `refs/notes/peers-attest` und `.peers/spine-runs/*.json`.
- Live-Stream folgt den Per-Peer-Dateien
  `.peers/log/peers/tick-<N>-<peer>.stream.jsonl`, wenn der Schalter
  `observability.tee_stream` an ist.
- Schreibt NUR sein eigenes Layout:
  `~/.config/peers-ctl/tui-layout.json`
  (bzw. `$XDG_CONFIG_HOME/peers-ctl/tui-layout.json`).

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Basis für das persistierte `tui-layout.json`.
- `PEERS_TEE_STREAM=1` — aktiviert den Live-Tee (äquivalent zu
  `observability.tee_stream: true` in `.peers/config.yaml`), damit codex /
  opencode im Live-Stream-Fenster live sichtbar sind (claude ist über sein
  Session-jsonl ohnehin live). Standardmäßig aus; fail-closed.
- `PEERS_PROJECTS_ROOT` — die Projekt-Registry-Wurzel, die das Cockpit liest.

## SIEHE AUCH
- `peers-ctl dashboard --help-man` — die nicht-interaktive Übersicht.
- `peers-ctl start --help-man` / `peers-ctl new --help-man` — die
  Schreib-Verben, die das Cockpit aufruft.
- `peers-ctl doctor --help-man` — die Vorprüfung, auf die der
  Start-Assistent gated.

## NOTES
- Tasten sind vim + Pfeile + Buchstaben; `?` öffnet den In-App-Hilfe-Screen.
  `[` / `]` blättern den Gate-History-Scrubber; `o` / `space` löst ein Panel
  in ein schwebendes Fenster, `x` schließt es, `f1` wechselt die Fenster.
- Die drei begleitenden Observability-Änderungen sind additiv und
  fail-closed: der Live-Tee ist standardmäßig aus (ein normaler Start ist
  byte-identisch); der Per-Tick-`gates`-Snapshot in `runs.jsonl` ist
  abwärtskompatibel (bestehende Leser ignorieren den Extra-Schlüssel); und
  die Registry `.peers/spine-runs/<mode_run>.json` ist nur-observierend.
