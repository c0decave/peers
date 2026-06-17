# peers

**Zwei AI-Coding-Agenten sind besser als einer — wenn man sie es beweisen lässt.**

peers lässt **n ≥ 2** AI-Coding-CLIs (Claude Code, Codex, …) als kooperierende
Peers laufen, die sich nicht einfach *einig* sind, dass etwas fertig ist —
sie müssen erst **harte, messbare Gates** bestehen: Tests grün, Coverage hält,
keine Regression, kein TODO/Stub/übersprungener Test, keine Secrets. Ein Peer
implementiert, der **andere reviewt blind** (ohne die Notizen des ersten zu
sehen), und ein **adversarialer Skeptiker** auditiert nach, bevor ein „fertig"
akzeptiert wird. Läuft **unbeaufsichtigt**, **budget-gedeckelt** und
**container-isoliert**.

**Warum das einen einzelnen Agenten im Loop schlägt:**

- **Gegated, nicht aus dem Bauch.** „Sieht fertig aus" konvergiert nie —
  *Gates grün + Skeptiker-clean* schon. Kein Konvergenz-Theater.
- **Blindes Peer-Review fängt Rubber-Stamping** — ein unabhängiges zweites
  Augenpaar, per Konstruktion.
- **Ein adversarialer Skeptiker jagt die Edge-Cases**, die deine Tests übersehen.
- **Unbeaufsichtigt & sicher:** Idle-Timeout-Überwachung, USD-/Tick-Budget-Caps,
  rootless cap-dropped Container, Egress-Allowlisting.

In einem instrumentierten Test baute peers einen Ausdrucks-Interpreter
greenfield *und* brownfield auf **0 Defekte über 50.000 zufällige
Testprogramme** — fing eingebaute Regressionen und fand selbst Edge-Case-Bugs,
die die Acceptance-Suite nie geprüft hat.

> English version: [README.md](README.md).

- **HOWTO: kompletter Audit + Fix an einer existierenden App**: [docs/HOWTO-audit-and-fix_DE.md](docs/HOWTO-audit-and-fix_DE.md) — [English version](docs/HOWTO-audit-and-fix.md)
- **`implement`-Mode (Feature aus PLAN.md bauen)**: [docs/MODES_IMPLEMENT_DE.md](docs/MODES_IMPLEMENT_DE.md) — [EN](docs/MODES_IMPLEMENT.md)
- Security-Modell: [docs/SECURITY_DE.md](docs/SECURITY_DE.md) — [EN](docs/SECURITY.md)

---

## Setup

### Voraussetzungen auf dem Host

- Python ≥ 3.11
- `git`
- `claude` CLI (Claude Code) — wird vom Tool aufgerufen
- `codex` CLI — wird vom Tool aufgerufen. Falls nicht auf `PATH`,
  vollen Pfad in `config.yaml` eintragen (siehe unten).
- Optional: `podman` und `podman compose` für den Container-Weg.

> **Hinweis zur Auth.** `~/.claude/` und `~/.codex/` müssen vorher
> existieren und gültige Tokens enthalten. Tokens werden bei Bedarf
> refreshed.

### Installation auf dem Host

```sh
git clone <repo-url> peers && cd peers
pip install -e .[dev]
pytest          # die volle Testsuite sollte grün sein
```

Damit sind `peers` und `peers-ctl` als Kommandos verfügbar.

### Installation im Container (Podman, empfohlen)

```sh
make build                                   # Image bauen
make init-target TARGET=/pfad/dein-projekt   # .peers/ im Ziel anlegen
make run TARGET=/pfad/dein-projekt           # Schleife starten
make status TARGET=/pfad/dein-projekt        # Stand zeigen
make shell TARGET=/pfad/dein-projekt         # bash im Container
make help                                    # alle Targets listen
```

Manuell, ohne Makefile:

```sh
podman build -f Containerfile -t peers:dev .
podman run --rm -it \
    --userns=keep-id \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    -v $PWD:/work \
    -v $HOME/.claude:~/.claude \
    -v $HOME/.codex:~/.codex \
    peers:dev run
```

Auf manchen Hosts schlägt das Default-`pasta`-Netzwerk fehl
(`/dev/net/tun: No such device`). Workaround: `make run NETWORK=host`.

---

## Einzelnes Projekt — eine Loop

```sh
cd /pfad/zu/dein-projekt
peers init
$EDITOR .peers/goals.yaml         # placeholder-replace-me ENTFERNEN, eigene Gates
python3 - <<'PY'
import hashlib, pathlib
p = pathlib.Path(".peers")
(p / "goals.sha256").write_text(hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n")
PY
peers run --max-ticks 20
peers status
tail -f .peers/log/runs.jsonl     # detailliertes Audit-Log pro Tick
peers replay <iter>               # JSON eines bestimmten Ticks
```

`peers init` schreibt `.peers/`, taggt `HEAD` als `peers-baseline`
(Rollback-Anker), snapshotet den `goals.yaml`-Hash und ergänzt
`.peers/` in der `.gitignore` des Zielprojekts.
Wenn du `.peers/goals.yaml` manuell editierst, aktualisiere danach
`goals.sha256`; die Loop hält absichtlich an, wenn sich Ziele ohne
explizite Bestätigung ändern oder `goals.yaml` während des Laufs
verschwindet.

### Treiber wählen

```sh
peers init --driver=hooks         # Stop-Hook-Snippets scaffolden
peers tmux up                     # tmux-Session für Sessions-Driver
```

`--driver=hooks` legt unter `.peers/hooks/` fertige Snippets für
`~/.claude/settings.json` und `~/.codex/config.toml`, sodass die
Stop-Hooks-Kette ohne manuelle JSON/TOML-Editierung gewired ist.

---

## Modi

Ein **Modus** ist ein wiederverwendbares Bündel aus Audit-Zielen +
Check-Skripten, das `peers-ctl new --modes=…` in `.peers/` ablegt. Modi
sind **stapelbar** (kommaseparierte Liste) — Ausnahmen: `describe` und
`implement` laufen eigenständig. Aktuelle eingebaute Modi:

| Modus | Was er tut |
|---|---|
| `audit` | Bug-Hunt + 3-Klassen-Testabdeckung + Secrets + Dependencies + API-Stabilität + Regression + Diff-Größe + Skip/xfail-Begründung. Fundament — fast immer nötig |
| `thorough` | Anti-Konvergenz-Theater-Hard-Gate: N=3 aufeinanderfolgende saubere Ticks + Skeptiker-Pass + Aggressive-Honesty-Soft-Goals. Stapelt auf `audit` |
| `describe` | iterativer Doku-Modus — Peers schreiben SPEC.md/ARCHITECTURE.md/DESIGN.md, bis N=2 nicht-substanzielle Doc-Commits. Vor dem Audit auf einem Repo ohne Docs; nicht mit Audit-Modi kombinierbar |
| `document` | erzeugt + pflegt maschinenlesbare Docs: eine `CODEMAP.yaml`, drift-gegated gegen den geparsten AST (jeder Eintrag trifft ein reales Symbol mit passender Signatur), plus `AGENTS.md` und `ARCHITECTURE.md` synchron dazu. Docs, die nicht stillschweigend verrotten; stapelbar oder eigenständig vor einem Audit |
| `implement` | End-to-end-Feature-Implementierung aus einer PLAN.md — eingefrorener Akzeptanz-Vertrag, Blind-Review zwischen Peers, Reviewer-Checkoffs, Honesty-/Cleanliness-Gates. Eigenständig; siehe [docs/MODES_IMPLEMENT_DE.md](docs/MODES_IMPLEMENT_DE.md) |

```sh
# Empfohlener Default für bestehenden Code:
peers-ctl new myapp --modes=audit,thorough

# Verifizierte, drift-gegatete Docs erzeugen (CODEMAP + AGENTS.md + ARCHITECTURE.md):
peers-ctl new myapp --modes=document

# Feature aus einer PLAN.md bauen (eigenständig):
peers-ctl new myfeature --container --modes=implement --plan ./PLAN.md
```

`peers-ctl modes list` zeigt immer den aktuellen eingebauten Satz.

## Operator-Workflows — `develop` und `research`

Neben der stapelbaren `--modes=…`-Audit-Loop bringt `peers` zwei
**One-Shot-Workflows** mit, die direkt über die innere `peers`-CLI laufen
(nicht über `peers-ctl new`). Beide arbeiten auf einem einzelnen Git-Repo,
das bereits einen konfigurierten Peer in `.peers/config.yaml` hat — sonst
einmal `peers init` —, steuern diesen Peer und legen ihr Ergebnis auf deinem
**aktuellen Branch** ab: kein Controller, kein langlebiges Run-Verzeichnis.

> Vollständige Operator-Referenz (Stages, Voraussetzungen, Honesty-Contract):
> [docs/WORKFLOWS_DEVELOP_RESEARCH_DE.md](docs/WORKFLOWS_DEVELOP_RESEARCH_DE.md).

### `peers develop` — dieses Repo autonom verbessern

Auditiert das Repo entlang der genannten Dimensionen, **friert aus den
übrig gebliebenen Findings einen Implement-Vertrag ein** und konvergiert
diesen zu einem **attestierten Commit** — dieselbe Blind-Review- +
Akzeptanz-Gate-Maschinerie wie `implement`, nur dass der Plan aus dem Audit
erzeugt wird statt aus einer handgeschriebenen `PLAN.md`.

```sh
cd /pfad/zu/deinem-repo
peers init                       # einmalig, falls .peers/ fehlt
peers develop . --dimensions correctness,security,perf
```

| Argument | Bedeutung |
|---|---|
| `repo` (positional) | Pfad zum Ziel-Git-Repository |
| `--dimensions` (Pflicht) | kommaseparierte Audit-Dimensionen, z. B. `correctness,security,perf` |
| `--peer <name>` | welcher konfigurierte Peer den Agenten treibt (Default: erster Peer in `.peers/config.yaml`) |
| `--convergence-budget <N>` | max. Implement-Versuche pro Vertrag, bevor aufgegeben wird (Default: 5) |

Für „finden UND fixen": Dimensionen wählen, weggehen, den attestierten
Commit reviewen, den es landet.

### `peers research` — zitierten Report aus einer `TOPIC.md` synthetisieren

Liest eine vom Operator verfasste **`TOPIC.md`** (ein `## Scope` +
`## Questions`-Brief) im Repo-Root, zerlegt sie in Teilfragen, sweept die
aktivierten Evidenz-Modalitäten nach belegenden Quellen und synthetisiert
eine **zitierte `RESEARCH.md`** aus den bestätigten Behauptungen — auf
deinen aktuellen Branch. Es ist ein generischer WISSENS-Workflow: ein
nicht-sicherheitsbezogenes Thema („Pflanzen klonen in Alaska") ist
erlaubt. Bricht GESCHLOSSEN ab bei fehlender/ungültiger `TOPIC.md`.

```sh
cd /pfad/zu/deinem-repo
cat > TOPIC.md <<'MD'
## Scope
Was beantwortet werden soll und die Grenzen der Frage.

## Questions
- Erste konkrete Frage?
- Zweite konkrete Frage?
MD
peers research . --modalities codebase,web
```

| Argument | Bedeutung |
|---|---|
| `repo` (positional) | Pfad zum Git-Repository (muss `TOPIC.md` enthalten) |
| `--modalities <liste>` | kommaseparierte Evidenz-Modalitäten: `codebase` (Default) und/oder `web` |
| `--peer <name>` | welcher konfigurierte Peer den Agenten treibt (Default: erster Peer) |

`codebase` belegt Behauptungen aus dem Repo selbst; `web` ergänzen, damit
der Agent Primärquellen-URLs zitiert. Jede tragende Behauptung in
`RESEARCH.md` ist zitations-gegated — unbelegte Behauptungen werden
verworfen, nie geraten.

## Mehrere Projekte — `peers-ctl`

`peers-ctl` ist der Host-seitige Controller, der mehrere Peers-Loops
parallel verwaltet — ohne Daemon. Jedes Projekt läuft als detached
Background-Prozess; der Controller speichert PIDs unter
`~/.config/peers-ctl/` und schützt mit `/proc`-basiertem
Starttime-Fingerprint gegen PID-Recycling.

### Pfad A — frisches Projekt ab null

```sh
peers-ctl new mything --spec /path/to/spec.md
$EDITOR ~/c0de/peers-c0de/mything/.peers/goals.yaml    # placeholder löschen, eigene Gates
cd ~/c0de/peers-c0de/mything && python3 - <<'PY'
import hashlib, pathlib
p = pathlib.Path(".peers")
(p / "goals.sha256").write_text(hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n")
PY
peers-ctl start mything --max-ticks 20 --max-usd 5
```

`peers-ctl new` macht in einem Schritt: Directory anlegen, git init,
initial-commit, `peers init` (inkl. peers-baseline-Tag und
.gitignore-Commit), `SPEC.md` schreiben (`--spec` als Text oder
existierender Dateipfad; pfadartig aussehende fehlende Werte wie
`./typo.md` werden abgelehnt), und das Projekt im Controller
registrieren.

**Projects-Root-Konvention**:

| Argument | Landet unter |
|---|---|
| `peers-ctl new mything` (bare name) | `$PEERS_PROJECTS_ROOT/mything` (default `~/c0de/peers-c0de/mything`) |
| `peers-ctl new /abs/path/foo` | `/abs/path/foo` (verbatim, backwards-compat) |
| `peers-ctl new sub/dir` (mit `/`) | relativ zur cwd |
| `PEERS_PROJECTS_ROOT=/work/peers peers-ctl new bar` | `/work/peers/bar` |

`peers-ctl doctor` zeigt den aktiven Root oben in der Ausgabe.

### Pfad B — bestehendes Projekt einbinden

```sh
peers-ctl add  /pfad/zu/projekt-a   --name a
peers-ctl add  /pfad/zu/projekt-b   --name b
peers-ctl doctor                  # tooling + per-Projekt-Config sanity-check
peers-ctl list

peers-ctl start a --max-ticks 20 --max-usd 3
peers-ctl status a
peers-ctl tail a                  # tail -f des Logfiles
peers-ctl review a                # letztes Handoff-Self-Review
peers-ctl stop a
peers-ctl prune                   # alte Logfiles löschen
```

Falls du in einem bestehenden Projekt `.peers/goals.yaml` vor dem
Start manuell änderst, aktualisiere auch dort `goals.sha256` wie im
Einzelprojekt-Beispiel oben.

### Automatische Hooks (Opt-out-Flags)

Standardmäßig aktiv, abschaltbar per Flag:

- **`recon`-Pre-Tick** (standardmäßig an): scannt das Repo einmal vor Tick 1 und schreibt `.peers/recon.md` (erkannte Sprachen, wichtige Docs, Entry-Point-Kandidaten, Top-Level-Baum). Kostenlos + schnell — kein LLM-Aufruf. Beseitigt die „blinder Tick 1"-Strafe. Abschalten: `peers-ctl start <name> --without-recon`.
- **`codemap`-Pre-Tick** (standardmäßig an): erstellt aus dem AST eine strukturelle CODEMAP und schreibt `.peers/CODEMAP.yaml` (maschinenlesbar: jedes öffentliche Symbol mit `file:line` + Signatur) und `.peers/codemap.md` (kompaktes, größenbegrenztes Digest, das die Peers als Kontext lesen). Kostenlos + schnell — kein LLM-Aufruf. Gibt den Peers vor Tick 1 die Form der öffentlichen API, zusätzlich zu recons Datei-Ebene. Abschalten: `peers-ctl start <name> --no-codemap`.

```sh
peers-ctl start <name> --without-recon
# Den substrate-only Pre-Tick-Recon-Schritt überspringen (kein LLM-Aufruf, kostenlos).

peers-ctl start <name> --no-codemap
# Den substrate-only Pre-Tick-CODEMAP-Schritt überspringen (kein LLM-Aufruf, kostenlos).
```

### Config-Datei-Optionen (`.peers/config.yaml`)

Einige Fähigkeiten sind per `.peers/config.yaml` opt-in (die generierte Datei
ist kommentiert; die wichtigsten):

- `graphify_mcp: true` — gibt den Peers einen opt-in, supply-chain-gekäfigten
  Code-Wissensgraphen, den sie statt `grep` über MCP abfragen (Aufrufer /
  Blast-Radius / kürzester Pfad / „wer nutzt X / wie erreicht A B"), damit
  Code-Navigation günstiger und präziser ist. Standardmäßig aus; **fail-open**
  (jeder Fehler läuft ohne Graph weiter, byte-identisch zu aus). Braucht
  `podman` + das `graphify-sandbox`-Image; `PEERS_CTL_NO_GRAPHIFY=1` erzwingt
  aus. Im `--container`-Modus teilt es das Egress/Auth-Proxy-Netzwerk.
- `egress_allow: ['^host\.example$', ...]` — zusätzliche Hosts, die die
  `--container`-Peers erreichen dürfen (tinyproxy-Host-Regexes, an die
  Egress-Allow-Liste angehängt, zusätzlich zu den LLM-API-Hosts), z.B. damit
  ein Peer eine Spezifikation oder Quelle abrufen kann. Standardmäßig aus;
  jedes Muster verankern.
- `observability.tee_stream: true` — **opt-in, standardmäßig aus.** Spiegelt
  den Live-stdout jedes Peers in eine tail-bare Datei
  `.peers/log/peers/tick-<N>-<peer>.stream.jsonl`, damit **codex / opencode im
  `peers-ctl tui` Live-Stream-Fenster live sichtbar** sind (claude ist über
  sein Session-jsonl ohnehin live). Auch ohne Config-Edit per
  `PEERS_TEE_STREAM=1` aktivierbar. Ein normaler Start mit dem Schalter aus ist
  byte-identisch; **fail-closed** — ein Tee-Fehler kann die Loop oder die
  Liveness/Idle-Erkennung nie stören, und die Stream-Dateien werden wie die
  übrigen Per-Tick-Logs rotiert.

### Container-Modus (`--container`)

Wenn z.B. codex auf dem Host nicht installiert ist, aber im
`peers:dev` Image vorhanden ist:

```sh
make build                        # einmalig
peers-ctl doctor                  # bestätigt podman + Image
peers-ctl start mything --container --max-ticks 20 --max-usd 5
```

Das startet `podman run -d --rm --name ... peers:dev run ...` und
trackt den laufenden Container per Name via `podman ps`. Die angezeigte
PID ist nur der Host-seitige `podman logs -f`-Streamer.
`peers-ctl stop --grace-s N` nutzt `podman stop -t N` und räumt danach
den Log-Streamer auf.

Der Container mountet Ziel-Repo, `~/.claude`, `~/.codex`, optional
`~/.claude.json` und optional `~/.gitconfig` read-only.
Vor dem Start vergleicht `peers-ctl` die Host-Package-Version mit
`peers --version` im Image: Minor-/Patch-Drift warnt, Major-Drift
bricht ab, bis das Image neu gebaut ist (`make build`).

`PEERS_CTL_IMAGE=name:tag` überschreibt den Image-Namen.

Jeder `start` spawnt einen unabhängigen detached Prozess. Stop
sendet SIGTERM → Gnadenfrist → SIGKILL, prüft via Starttime, ob
die PID wirklich noch zu unserem Loop gehört.

---

## n-peer-Konfiguration

`config.yaml` akzeptiert eine geordnete `peers:`-Liste. Default sind
2 Peers (`claude` + `codex`). Drei oder mehr sind problemlos
möglich.

```yaml
peers:
  - name: claude
    tool: claude
    model: opus        # optional; weglassen = CLI-Default
    reasoning: high    # claude: low|medium|high|xhigh|max
    argv: ["claude", "-p", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute

  - name: codex
    tool: codex
    model: gpt-5.1-codex-max
    reasoning: xhigh   # codex: minimal|low|medium|high|xhigh
    provider: openai   # openai|openrouter
    argv: ["codex", "exec", "{PROMPT}"]
    prompt_mode: argv-substitute

  # Dritter Peer:
  - name: claude-2
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute
```

Die alte `tools: {claude: …, codex: …}`-Mapping wird weiterhin
gelesen und transparent ins neue Schema übersetzt (backward compat).

`model`, `reasoning` und `provider` sind optionale Convenience-Felder.
Explizite `argv`-Flags gewinnen weiterhin. Beim Scaffolden kannst du
sie direkt setzen:

```sh
peers-ctl new meine-app --modes=audit \
  --peer-model claude=opus \
  --peer-provider codex=openrouter \
  --peer-model codex=~openai/gpt-latest \
  --peer-reasoning codex=xhigh
```

Für OpenRouter vor `peers run`, `peers tick`, `peers tmux up` oder
`peers-ctl start` `OPENROUTER_API_KEY` exportieren; diese Kommandos
brechen früh ab, wenn der Key fehlt. Im Container-Modus wird nur der
Env-Name durchgereicht und `openrouter.ai` nur für opt-in-Projekte in
der Egress-Proxy-Allowlist geöffnet.

**Name-Validation:** `[A-Za-z0-9][A-Za-z0-9_-]{0,31}` — keine
Path-Traversal-Zeichen, keine Shell-Metachars, keine tmux-Ambiguität.

### opencode-Peers + lokale Modelle (ollama / vllm / llama.cpp)

`opencode` ist ein First-Class-Tool neben `claude` und `codex`. Mit
`--format json` bekommt das Substrate denselben strukturierten Kanal wie bei
den anderen — Token-/USD-Abrechnung (aus `step-finish`-Events) und
echo-immune Auth/Quota-Halt-Erkennung (aus `error`-Events):

```yaml
peers:
  - name: opencode
    tool: opencode
    model: ollama/qwen2.5      # opencodes <provider>/<model> (KEIN separates provider:)
    reasoning: high            # → --variant high
    argv: ["opencode", "run", "--format", "json", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute
```

opencode ist auch der einfachste Weg zu **lokalen Modellen** — ein universeller
Gateway: den Backend einmal in opencodes eigener Config einrichten
(`opencode providers` bzw. `opencode.json`) — ollama, vllm, llama.cpp,
LM Studio oder jeden OpenAI-kompatiblen `/v1`-Endpoint — dann `model` auf
`<provider>/<model>` zeigen lassen:

```yaml
    model: ollama/qwen2.5            # lokal via ollama
    model: openai-compatible/<name> # lokaler vllm-/llama.cpp-Server
    model: anthropic/claude-...      # Cloud, über opencode
```

Das Substrate braucht **keinen** lokal-spezifischen Code; opencode löst den
Provider auf. Hinweise:

- `provider:` wird für opencode **nicht** genutzt — den Provider im `model`
  kodieren (`provider/model`). Ein gesetztes `provider:` wird abgelehnt.
- Billing ist für opencode **warn**, nie ein harter `max_usd`-Kill (lokal =
  gratis, opencode-hosted = Abo, BYOK-Cloud = metered — der Tool-Name allein
  sagt es nicht, also greift der konservative Default).
- `codex` erreicht lokale Modelle nur via `--oss --local-provider ollama|lmstudio`
  oder einen Custom-Provider mit der OpenAI-**Responses**-API
  (`wire_api=responses`) — codex hat die chat-API fallengelassen, daher laufen
  chat-only-Server (llama.cpp, vanilla-ollama) über opencode.

---

## Reviewer-Modi (Soft-Goals)

```yaml
goals:
  - id: docs-complete
    type: soft
    prompt: "Are all public docs current? Reply JSON."
    reviewer: other        # or: both | alternating | quorum
    consensus_needed: 2
    review_interval: 5

  - id: api-coherence
    type: soft
    prompt: "Per-opcode behavioral check. Reply JSON."
    reviewer: quorum
    quorum: "2/3"          # 2 von letzten 3 Reviews müssen pass:true sein
```

- `other` (Default) — irgendein nicht-aktiver Peer reviewt.
- `both` — JEDER Peer muss `consensus_needed` pass:true-Reviews
  abgeben.
- `alternating` — Review-Pflicht rotiert pro Review ein Slot weiter.
- `quorum` + `quorum: "N/M"` — ≥N der letzten M Reviews pass:true.

---

## `goals.yaml` an dein Projekt anpassen

Nach `init` enthält die Datei fünf Default-Ziele plus den absichtlichen
Hard-Fail `placeholder-replace-me`. **Wichtig:**
`placeholder-replace-me` muss gelöscht werden, sonst läuft das Tool
ewig (so beabsichtigt: ohne echte Ziele kein "fertig").

```yaml
goals:
  - id: self-review-on-handoff
    type: hard
    cmd: "python3 -m peers.templates.modes.audit.checks.verify_self_review"
    pass_when: "exit_code == 0"

  - id: tests-pass
    type: hard
    cmd: "pytest -q"
    pass_when: "exit_code == 0"

  - id: coverage-80
    type: hard
    cmd: "pytest --cov=src --cov-report=json -q"
    pass_when: "json('coverage.json').totals.percent_covered >= 80"

  - id: typecheck
    type: hard
    cmd: "mypy src/"
    pass_when: "exit_code == 0"
```

### `pass_when`-DSL

Eine kleine, sichere Teilsprache (kein beliebiges Python; `__class__`
& Co. blockiert). Erlaubt:

- `exit_code == 0`
- `regex('PATTERN', stdout) == None`
- `json('relativer/pfad.json').a.b.c >= 80`
  — **gesandboxt:** nur Pfade innerhalb des Zielprojekts; kein
  `/etc/passwd`, kein `../escape`, keine Symlinks/Hardlinks; 2 MiB
  Lesecap.
- `int(stdout.strip()) < 5`
- BoolOp / Compare-Ketten / einfache Methoden auf stdout/stderr
  (`.strip()`, `.splitlines()`, …)

`stdout`/`stderr`, die der DSL ausgesetzt werden, sind gecappt;
String-Literale und Regex-Patterns sind begrenzt, und `regex()` läuft
mit Timeout.

---

## Was die Substrate garantiert

- **State-Durability.** `state.json` wird atomic geschrieben
  (tmp + fsync + rename + parent-dir-fsync). Migration von v1 → v2
  legt eine `.pre-migration`-Sicherung an.
- **Self-Review-Pflicht.** Standard-Hard-Gate `self-review-on-handoff`
  prüft, ob jedes Handoff-Commit eine `## Self-Review`-Sektion und
  einen `Self-Review: pass`-Trailer trägt. Das Default-Gate nutzt den
  vertrauenswürdigen Package-Checker statt einer projektlokal
  veränderbaren Kopie.
- **Anti-Cheating Hard-Block.** Ein Tick, der NUR Test-Files ändert,
  wird via `git revert --no-commit` + Commit rückgängig gemacht, der
  Tick gilt als Fail, der Peer behält den Turn, und die Warnung
  landet im nächsten Prompt. Zwei Reverts in Folge → Peer ist
  `degraded`.
- **Goal-Mutation-Lock.** sha256-Snapshot der `goals.yaml` wird vor
  jedem Tick mit no-follow Reads verifiziert. Mid-Loop-Änderungen
  halten die Schleife mit klarer Reason; eine gelöschte `goals.yaml`
  zählt ebenfalls als Mutation.
- **Lock-Status-Klarheit.** `run.lock` bleibt nach Unlock absichtlich
  liegen, damit alle Contender denselben Inode nutzen. `peers status`
  prüft per `flock`, ob der Lock aktiv gehalten wird oder nur stale ist.
- **Control-Plane-File-Hardening.** State, Logs, Reports,
  Verify-Ausgaben, Controller-Registry und Controller-Logs verweigern
  Symlinks, nicht-reguläre Dateien und Hardlinks. Log-Appends öffnen
  das Parent-Directory no-follow, um späte Parent-Symlink-Swaps zu
  blocken. State-, Goals- und Controller-Registry-Reads sind vor dem
  JSON/YAML-Parsing größenbegrenzt.
- **PID-Recycle-Schutz.** `peers-ctl` speichert beim Start
  `/proc/<pid>/stat`-Starttime; vor jedem Signal wird verifiziert.
- **File-Channel race-safe.** Hybrid-Comm `send()` schreibt zuerst in
  eine temporäre Dotfile und veröffentlicht per atomic link, sodass
  Consumer keine halben Nachrichten sehen. Peer-Namen werden gegen
  Path-Traversal validiert.
- **Audit-Trail.** `runs.jsonl` enthält pro Tick:
  `soft_fail_reason`, Tokens & USD pro Tick + Total, `head_before/after`,
  `peer_state_after`, `warnings_emitted`, `truncated`-Flag.

---

## Health-Modell

Wir können nicht vorhersagen, wie lange ein Peer an einer Aufgabe
arbeitet. Statt fester Wallclock-Timeouts misst das Tool, ob noch
etwas produziert wird:

- Jeder Byte auf stdout/stderr setzt die Idle-Deadline zurück.
- Erst nach echter Stille (default 15 min ohne Output) wird der Peer
  als festgefahren eingestuft (`classification: idle-timeout`).
- `absolute_max_runtime_s` ist nur die Notbremse für komplett
  durchgedrehte Prozesse.
- `error_patterns` killt sofort bei klaren Fehlersignalen
  (Rate-Limit, Auth-Fehler, API 5xx).

---

## Beobachtung im laufenden Betrieb

```sh
peers status
# Iteration, Whose-Turn, Goal-Status, Tool-Health, Budget %,
# dirty-tree, HALTED, Lock-Info, recent_fails, Warnungen, Log-Line-Count

tail -f .peers/log/runs.jsonl
# eine JSON-Zeile pro Tick mit reichhaltigen Audit-Feldern

peers-ctl dashboard --live
# Multi-Projekt-Live-Ansicht mit Alerts und neuesten Events

peers-ctl dashboard --project mein-projekt
# Single-Projekt-Drilldown mit jüngsten Runs und Bug-Reports

git log --oneline
# echte Code-Änderungen der Peers, jede mit eigenen Trailern

cat .peers/HALTED.md
# falls beide / alle Peers degraded sind, steht hier die Diagnose
```

### `peers-ctl tui` — Live-Cockpit auf dem Host

```sh
pip install -e .[tui]      # einmalig: das optionale TUI-Extra installieren
peers-ctl tui              # das Host-seitige Live-Cockpit starten
```

Ein dunkles, zustandsgefärbtes Master-Detail-„Mission Control" für eine
peers-Flotte: Projekte starten, den Agenten bei der Arbeit zusehen, lesen,
was sie sagen und wie sie sich gegenseitig prüfen, und Gates / Steps / Tasks,
gefundene Bugs sowie erzeugte Diffs sehen — plus ein vorausschauender Blick
auf die agentic-os-Autonomie-Schicht.

- **Optionales Extra.** Das TUI ist ein Textual-UI hinter dem optionalen
  `[tui]`-Extra (`pip install -e .[tui]` zieht Textual + textual-window) —
  der Kern bleibt `pyyaml`-only. Ohne das Extra gibt `peers-ctl tui` einen
  freundlichen Installationshinweis aus und beendet sich, ohne Absturz.
- **Nur lesend; handelt über das Substrat.** Das Cockpit *liest* nur die
  dateibasierten Signale (`projects.yaml`, Per-Run-Zustand,
  git-Trailer/Attestierung, `bugs.jsonl`, `runs.jsonl`, das Spine-Ledger).
  Jede *Aktion* ruft die bestehenden `peers-ctl`-Verben auf, damit die Guards
  und Hash-Ketten des Substrats maßgeblich bleiben — das TUI implementiert
  keine Schreib-Logik neu, schreibt nie in `.peers/` und fügt keine neue
  Vertrauensfläche hinzu. CONVERGED- / Gate- / Integritäts-Urteile werden
  stets aus dem Substrat **neu abgeleitet** und trauen nie dem
  agent-beschreibbaren gespeicherten `independence`-Flag.
- **Fenster.** Eine Fleet-Seitenleiste plus verschiebbare / vergrößerbare /
  ein-/ausblendbare + herauslösbare Fenster — Peers, Gates (mit
  History-Scrubber: mit `[` / `]` durch vergangene Ticks blättern, mit
  absoluter + relativer Zeit), Tasks/Steps, Live-Stream, Tick-Verlauf,
  Budget, Bugs, Konsens/Attestierung (mit Fälschungs-Badge), Log, Diff — plus
  vorausschauende Autonomie-Fenster (Autonomie-Ledger, Spine-Gates,
  Propagations-DAG, Autonomie-Feed, Eskalations-Banner), die einen ehrlichen
  Leerzustand zeigen, bis das Spine an einen operator-startbaren Modus
  angebunden ist.
- **Sicheres Handeln.** Ein doctor-geprüfter, off-thread laufender
  Start-Assistent legt Projekte an und startet sie; Eingriffs-Dialoge
  (stop / resume / ack-block / amend) zeigen das exakte Verb und verlangen
  Tippen-zum-Bestätigen bei vertrags-berührenden Operationen.
- **Tasten + Layout.** vim + Pfeile + Buchstaben (`?` für die In-App-Hilfe);
  das Layout wird in `~/.config/peers-ctl/tui-layout.json` persistiert.

Gespeist wird das Cockpit von drei additiven, fail-closed Substrat-Erweiterungen:
der **Live-Tee** (`observability.tee_stream`, opt-in, siehe oben) für live
codex/opencode-Streams; dem **Per-Tick-`gates`-Snapshot** in `runs.jsonl`
(always-on, abwärtskompatibel) für den Gate-History-Scrubber; und der
**`.peers/spine-runs/<mode_run>.json`-Registry** (nur-observierend), die die
Autonomie-Fenster aufleuchten lässt, sobald das Spine operator-startbar wird.

---

## Sauber abbrechen

`Ctrl-C` — der Driver persistiert den State (atomic write + fsync),
killt den laufenden Peer-Subprozess sauber und beendet sich. Beim
nächsten `peers run` wird die Schleife mit erhaltenem Budget und
Goal-Stand fortgesetzt.

Über `peers-ctl stop <name>` läuft dasselbe Spiel als Background-
Prozess: SIGTERM → 10 s Gnadenfrist → SIGKILL falls nötig.

---

## `max_usd_mode` — OAuth vs API-Key

`claude` (Claude Code) und `codex` (ChatGPT-bundled) authentifizieren
sich per **OAuth → flat subscription**. Ihr `total_cost_usd`-Feld zeigt
den *API-äquivalenten* Preis; der Nutzer zahlt $0 inkrementell. Ein
*hard* Budget-Cap ist da sinnlos — würde einen bezahlten Lauf killen.

`max_usd_mode` steuert die Policy:

| mode | Verhalten |
|------|-----------|
| `auto` (default) | inspect `~/.claude/.credentials.json` + `~/.codex/auth.json` (`auth_mode`). Alle peers OAuth → `warn`; jeder peer mit API-Key → `hard`. |
| `hard` | Exit bei Cap (pre-Phase-3i Verhalten). Bei explizitem `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| `warn` | One-time warning beim Threshold; KEIN Exit. |
| `off`  | `max_usd` komplett ignorieren. |

`peers info` zeigt den *resolved* Mode + Grund:

```
budget:  iterations≤20, runtime≤10800s, USD≤$25.0
  max_usd_mode=warn (auto: all peers OAuth-billed)
```

Pre-Reqs für Token-Tracking bei claude: argv um
`"--output-format", "json"` erweitern (sonst sieht der substrate $0):

```yaml
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "json", "{PROMPT}"]
    prompt_mode: argv-substitute
```

---

## Bug-Hunt-Protokoll

Jedes `peers init` liefert fünf Default-Ziele plus den absichtlichen
`placeholder-replace-me` Hard-Fail. Die Default-Ziele zwingen
Self-Review und gegenseitiges Bug-Hunting, bevor "fertig" erreicht
wird:

| Gate | Type | Pass when |
|------|------|-----------|
| `self-review-on-handoff` | hard | jeder Handoff-Commit hat `## Self-Review` und `Self-Review: pass` |
| `bug-hunt-clean` | hard | 0 unresolved bugs an severity `crit`/`high`/`med` |
| `bug-hunt-round-1` | soft (`consensus_needed: 2`) | jeder Peer sagt "round 1 done" |
| `bug-hunt-round-2` | soft (`consensus_needed: 2`) | jeder Peer sagt "round 2 done" nach round-1 fixes |
| `test-coverage-3-class` | soft (`consensus_needed: 2`) | jeder Peer hat die Tests des anderen auf happy/edge/sad-coverage reviewt |

Bug-Filing-Commit-Schema:

```
BUG-007: null deref in parser

## Bug-Report
{"id":"BUG-007","severity":"high","fix_by":"codex",
 "location":"src/parser.py:42",
 "description":"Crashes on empty input; expected: return None."}

Peer: claude
Bug-Report: BUG-007
```

Resolution-Commit-Schema:

```
Resolve BUG-007

## Bug-Resolution
{"resolves":"BUG-007","status":"fixed","note":"guarded with if not s: return"}

Peer: codex
Bug-Resolves: BUG-007
```

Status checken:

```sh
python3 -m peers.bug_hunt summary           # human rollup
python3 -m peers.bug_hunt gate /path/to/repo  # exit 0 iff clean
peers verify                                # re-runs hard gates incl. bug-hunt-clean
```

Severity-Ladder: `crit` (Datenverlust / RCE) > `high` (Feature broken)
> `med` (degraded UX) > `low` (Nit) > `info` (Notiz). Nur die top 3
blocken die Completion.

---

## `peers verify` — Gates ohne Peer-Loop re-run

Nach `peers run` (oder bei jedem späteren check-out) kannst du alle
hard-goals gegen den aktuellen Stand re-runnen, ohne einen Peer
hochzufahren:

```sh
peers verify           # exit 0 iff alle gates pass; schreibt .peers/VERIFY.md
```

Zusätzliche custom commands über `verify.commands` in config.yaml:

```yaml
verify:
  timeout_s: 60
  commands:
    - name: cli-help
      cmd: "PYTHONPATH=src python -m mything --help"
    - name: ui-screenshot
      cmd: "xvfb-run -a python tools/screenshot.py out.png"
      timeout_s: 30
```

`peers verify` nutzt für Hard-Goals `goals.timeout_s`, außer
`verify.timeout_s` überschreibt ihn. `verify.commands` zählen mit
Exit-Code 0 als pass; non-zero oder Timeout ist fail.

---

## `api-error` Diagnostik

Wenn ein Peer-Process mit `classification: "api-error"` exittet, ent-
hält der `runs.jsonl` Eintrag:

```json
"matched_error_pattern": "Authentication failed",
"matched_error_snippet": "Authentication failed: token expired ..."
```

Plus für ALLE non-success ticks:

```json
"stderr_tail": "...last 800 bytes of stderr...",
"stdout_tail": "...last 400 bytes of stdout..."
```

Soft-Review-Ticks schreiben zusätzlich `soft_reviews_seen`,
`soft_reviews_ingested` und `soft_reviews_rejected`.

So findest du die Ursache ohne ins Container-Log zu grepen.

---

## `peers init --driver=hooks --install`

Standard-`--driver=hooks` schreibt nur Snippets nach `.peers/hooks/`.
Mit `--install` (nur mit `--driver=hooks`) wird der Stop-Hook
**direkt** in `~/.claude/settings.json` und `~/.codex/config.toml`
gemergt:

```sh
peers init --driver=hooks --install
```

Eigenschaften:
- **idempotent** — Re-Run druckt `noop`, dupliziert nichts. Jeder
  Eintrag ist mit `# peers:<absolute-target-path>` getaggt.
- **drift-aware** — wenn das Projekt umzieht, wird der alte Eintrag
  in-place ersetzt (mit Backup `*.bak.peers-<TS>`).
- **conservative bei TOML** — wenn `~/.codex/config.toml` schon ein
  `[hooks]` ohne unseren Marker hat, refused der installer und druckt
  einen Hinweis.

---

## Phase-3i-Zustand (2026-05-21)

- Die volle Testsuite ist grün; feste Testzahlen stehen im jeweiligen
  Release-/CI-Lauf.
- n-peer-Foundation: configurable, default n=2; live-validiert mit n=3.
- Reviewer-Modi: `other`, `both`, `alternating`, `quorum`.
- Anti-Cheating: Hard-Block mit Revert.
- `peers-ctl`: Multi-Project-Controller mit PID-Recycle-Defence.
- Goals-DSL `json()` gesandboxt und gecappt, `pass_when` jetzt
  frühvalidiert bei `peers init`/`peers info`/`peers run`.
- Hybrid-Comm: write-AND-read, race-safe, archive direction-namespaced
  und atomic publiziert.
- Hook-Driver: `--install` auto-patcht host-config mit Backup.
- OAuth-aware `max_usd_mode: auto` — default auf `warn` bei
  Subscription-Setup.
- Bug-Hunt-Protokoll als first-class hard+soft Gates.
- Audit-Trail: enriched `runs.jsonl` mit `matched_error_pattern`,
  `matched_error_snippet`, `stderr_tail`, `stdout_tail`,
  `soft_reviews_seen/ingested/rejected`.

Weiterführend:
- [docs/HOWTO-audit-and-fix_DE.md](docs/HOWTO-audit-and-fix_DE.md) — Audit + Fix einer existierenden Anwendung (deutsche Anleitung)
- [docs/MODES_IMPLEMENT.md](docs/MODES_IMPLEMENT.md) — Operator-Referenz für `implement`-Mode
- [docs/SECURITY.md](docs/SECURITY.md) — Threat-Model + Layer-Mitigations
