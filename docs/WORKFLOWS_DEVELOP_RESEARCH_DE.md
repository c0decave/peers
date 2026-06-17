# Operator-Workflows: `peers develop` und `peers research`

> Zwei **One-Shot-Workflows**, die direkt über die innere `peers`-CLI laufen.
> Anders als die stapelbare `--modes=…`-Audit-Loop (via `peers-ctl`) laufen
> diese einmal gegen ein einzelnes Repo, steuern dessen konfigurierten Peer und
> legen ihr Ergebnis auf deinem **aktuellen Branch** ab — kein Controller, kein
> langlebiges Run-Verzeichnis.
>
> README-Kurzfassung:
> [README_DE.md](../README_DE.md#operator-workflows--develop-und-research).
> Die englische Ausgabe: [WORKFLOWS_DEVELOP_RESEARCH.md](WORKFLOWS_DEVELOP_RESEARCH.md).

---

## Voraussetzungen (beide)

Beide Workflows arbeiten auf einem Git-Repo, das bereits einen konfigurierten
Peer in `.peers/config.yaml` hat:

```sh
cd /pfad/zu/deinem-repo
peers init                 # schreibt .peers/ (einmalig); committet .gitignore
$EDITOR .peers/config.yaml # mindestens einen Peer benennen (z. B. claude / codex)
```

`peers-ctl doctor` prüft, dass Peer-CLIs + git nutzbar sind. Beide Workflows
brechen **GESCHLOSSEN** ab (Exit ≠ 0, ehrliche Meldung) bei einem Nicht-Git-Repo,
fehlender `.peers/config.yaml` oder ungültigem Input — nie ein erfundenes
Ergebnis.

---

## `peers develop` — dieses Repo autonom verbessern

```sh
peers develop . --dimensions correctness,security,perf
```

Treibt die echte **AUDIT → VERIFY → AUTHOR → IMPLEMENT**-Naht über ein Repo:

1. **AUDIT** — der konfigurierte Peer auditiert das Repo entlang jeder
   genannten `--dimension` und legt Kandidaten-Findings offen.
2. **VERIFY** — jedes Finding wird **adversarial verifiziert** (`verify_claim`
   der Spine), bevor es eine Änderung treiben darf — ein falsch etikettierter
   oder widerlegter „Bug" wird nie ins Tool gefixt.
3. **AUTHOR** — die übrig gebliebenen Findings werden zu einem
   **Implement-Vertrag** eingefroren (dieselbe Frozen-Acceptance-Contract-
   Maschinerie wie `implement`), erzeugt aus dem Audit statt aus einer
   handgeschriebenen `PLAN.md`.
4. **IMPLEMENT** — der Vertrag wird zu einem **attestierten Commit**
   konvergiert, über Blind-Review- + Akzeptanz-Gates, bis zu
   `--convergence-budget` Versuche.

### Flags

| Argument | Default | Bedeutung |
|---|---|---|
| `repo` (positional) | — | Pfad zum Ziel-Git-Repository |
| `--dimensions` | **Pflicht** | kommaseparierte Audit-Dimensionen, z. B. `correctness,security,perf` |
| `--peer <name>` | erster Peer in `.peers/config.yaml` | welcher konfigurierte Peer den Agenten treibt |
| `--convergence-budget <N>` | `5` | max. Implement-Versuche pro Vertrag, bevor aufgegeben wird |

### Ergebnis

- Attestierte Commit(s) auf deinem **aktuellen Branch** (jeder trägt die
  Substrate-eigene Authorship-Attestation — der Attestation vertrauen, nicht dem
  Trailer).
- Ein Run-Ledger unter `.peers/run.jsonl` (Tick-für-Tick-Protokoll).
- Exit `0` bei konvergiertem, attestiertem Ergebnis; ≠ 0 bei einem
  fail-closed-Stopp.

**Dafür:** wenn das Substrate *finden UND fixen* soll — Dimensionen wählen,
weggehen, dann den gelandeten Commit reviewen.

---

## `peers research` — zitierten Report aus einer `TOPIC.md` synthetisieren

```sh
cat > TOPIC.md <<'MD'
## Scope
Was beantwortet werden soll und die Grenzen der Frage.

## Questions
- Erste konkrete Frage?
- Zweite konkrete Frage?
MD
peers research . --modalities codebase,web
```

Liest eine vom Operator verfasste **`TOPIC.md`** im Repo-Root und fährt
**INTAKE → DECOMPOSE → SWEEP → SYNTHESIZE**:

1. **INTAKE** — `require_topic` validiert `TOPIC.md`: nicht-leeres `## Scope` +
   `## Questions` (ein `## Frameworks`-Abschnitt ist **nicht** nötig — research
   ist ein generischer WISSENS-Workflow, ein nicht-sicherheitsbezogenes Thema
   wie „Pflanzen klonen in Alaska" ist erlaubt). Fail-closed + symlink-ablehnend,
   mit 2-MiB-Cap und einem Zeichen-Mindestmaß pro Abschnitt.
2. **DECOMPOSE** — das Thema wird in konkrete Teilfragen zerlegt.
3. **SWEEP** — jede aktivierte Modalität wird nach belegenden Quellen gesweept.
4. **SYNTHESIZE** — eine **zitierte `RESEARCH.md`** wird aus den bestätigten
   Behauptungen geschrieben.

### Flags

| Argument | Default | Bedeutung |
|---|---|---|
| `repo` (positional) | — | Pfad zum Git-Repository (muss `TOPIC.md` enthalten) |
| `--modalities <liste>` | `codebase` | kommaseparierte Evidenz-Modalitäten: `codebase` und/oder `web` |
| `--peer <name>` | erster Peer in `.peers/config.yaml` | welcher konfigurierte Peer den Agenten treibt |

`codebase` belegt Behauptungen aus dem Repo selbst; `web` ergänzen, damit der
Agent Primärquellen-URLs zitiert.

### Honesty-Contract (worauf der Report gegated ist)

`RESEARCH.md` ist **zitations-gegated**, keine freie Prosa:

- **CITED** — tragende Behauptungen brauchen mindestens `MIN_CITATIONS` (2)
  verschiedene Primärquellen-URLs; unbelegte Behauptungen werden **verworfen,
  nie geraten**.
- **GAPS** — ein nicht-trivialer `## Gaps`-Abschnitt ist Pflicht (was der Lauf
  *nicht* beantworten konnte, wird berichtet, nicht versteckt).
- **Kein Vollständigkeits-Theater** — „umfassend"/„erschöpfend"-Behauptungen
  sind verboten; der Report nennt, was er tatsächlich belegt hat.

### Ergebnis

- `RESEARCH.md` auf deinem **aktuellen Branch**.
- Exit `0` bei synthetisiertem Report; ≠ 0 (mit berichtetem Stop-Grund) bei
  fehlender/ungültiger `TOPIC.md` oder leerem Evidenz-Sweep.

**Dafür:** wenn du eine prüfbare, quellenbelegte Antwort auf einen geschriebenen
Brief willst — ohne dass das Modell Lücken mit Vermutungen auffüllt.
