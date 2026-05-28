# HOWTO: App komplett auditieren + fixen mit peers

> Ziel: eine bestehende App von zwei oder mehr LLM-peers (claude + codex)
> komplett durchprüfen lassen — jeder Bug gefunden, kritisch bewertet,
> dann ehrlich gefixt, mit happy/edge/sad-Tests dokumentiert, alles
> committed und gepusht. Keine Abkürzungen.

Die Anleitung ist das Resultat aus mehreren echten Dogfood-Runs auf
Python- und JS-Projekten.

> The English edition lives at
> [HOWTO-audit-and-fix.md](HOWTO-audit-and-fix.md).

---

## 0) Prerequisites (einmal pro Host)

```sh
cd <path-to-your-peers-checkout>
pip install -e .[dev]                 # peers + peers-ctl auf PATH
podman build --network=host -f Containerfile -t peers:dev .
peers-ctl doctor                      # sanity-check
```

`peers-ctl doctor` muss anzeigen:
- `claude` gefunden (oder dein VSCode-Extension-Pfad)
- `codex` gefunden
- `peers:dev` Image gebaut
- `podman` ≥ 4.0
- Auth-Files vorhanden: `~/.claude/`, `~/.claude.json`, `~/.codex/`

Wenn Auth fehlt: `claude login` und `codex auth login` ausführen.

---

## 1) Projekt anmelden

```sh
# Bare-Name landet in $PEERS_PROJECTS_ROOT (default ~/c0de/peers-c0de/).
# Voller Pfad bleibt verbatim.
peers-ctl new myapp --container --modes=audit --spec ./myapp-spec.md
```

`myapp-spec.md` ist eine Markdown-Beschreibung von:
- **Was die App tut** (1–2 Absätze)
- **Was "fertig" heißt** (concrete acceptance criteria, kein "ungefähr")
- **Was sicher NICHT in Scope ist** (negative scope)
- **Bekannte Schwachstellen, die die peers untersuchen sollen**
- **Performance-Hotpaths**: Pfade die `perf-no-regression` benchmarken
  soll (z.B. "die `parse_message`-Funktion in src/wire.py")
- **Öffentliche API**: was darf NICHT versehentlich brechen — Liste
  der exportierten Funktionen/Klassen/CLI-Flags (Liste hier macht
  `api-stable`-Snapshots erst sinnvoll)

Je präziser SPEC.md ist, desto fokussierter der Audit. Vage SPECs
führen zu kosmetischen Bug-Reports.

Fuer JavaScript/TypeScript-Projekte:

```sh
peers-ctl new myapp --container --modes=audit --lang=js --spec ./myapp-spec.md
```

Unterstuetzt sind aktuell `python` (Default) und `js`. Andere Werte
warnen und fallen auf Python-Templates zurueck, damit Scaffolding nie an
einem Tippfehler scheitert.

Was angelegt wird:
```
~/c0de/peers-c0de/myapp/
├── .peers/
│   ├── config.yaml         # peer-Setup + Budget + Health
│   ├── goals.yaml          # audit hard + soft goals (anpassen erlaubt)
│   ├── SPEC.md             # Kopie deiner Spec
│   ├── checks/             # audit Check-Scripts aus dem Template
│   └── log/runs.jsonl      # tick-by-tick JSON, wird beim Lauf gefüllt
```

---

## 2) goals.yaml — das eigentliche Audit-Programm

Ersetze das Default-Scaffold mit Folgendem (anpassen an deinen
Tech-Stack). Die einzelnen Goals sind **bewusst hart konfiguriert** —
die peers haben keinen Ausweg über "ach das ist halt komplex".

```yaml
goals:
  # ===== HARTE GATES — alle müssen grün für "complete" =====

  - id: self-review-on-handoff
    type: hard
    description: "Jeder handoff-Commit trägt eine self-review."
    cmd: "python3 -m peers.templates.modes.audit.checks.verify_self_review"
    pass_when: "exit_code == 0"

  - id: tests-pass
    type: hard
    description: "Die volle Test-Suite ist grün."
    cmd: "python3 -m pytest -q 2>&1 || true"      # passe an dein Tool an
    pass_when: |
      regex('failed', stdout) == None
        and regex('passed', stdout + stderr) != None

  - id: tests-cover-happy-edge-sad
    type: hard
    description: "Jede non-trivial Code-Datei in src/ hat mindestens
      einen happy + edge + sad Test (via .peers/checks/coverage_3class.py)."
    cmd: "python3 .peers/checks/coverage_3class.py src tests"
    pass_when: "exit_code == 0"

  - id: lint-clean
    type: hard
    cmd: "ruff check . 2>&1 || true"              # oder eslint/clippy/...
    pass_when: "regex('error', stdout + stderr) == None"

  - id: type-clean
    type: hard
    cmd: "mypy src/ 2>&1 || true"                 # nur wenn dein Projekt typed ist
    pass_when: "regex('error', stdout + stderr) == None"

  - id: bug-hunt-clean
    type: hard
    description: "0 offene Bugs an severity crit/high/med.
      `Bug-Defer:`-mit-Begründung gilt als geschlossen."
    cmd: "python3 -m peers.bug_hunt gate ."
    pass_when: "exit_code == 0"

  - id: tdd-reproduces-bug
    type: hard
    description: "Jeder Bug-Resolves an blocking-Severity hat einen
      VORANGEHENDEN Bug-Reproduce-Commit (failing test first)."
    cmd: "python3 -m peers.bug_hunt gate-tdd ."
    pass_when: "exit_code == 0"

  - id: no-secrets-committed
    type: hard
    description: "Keine Credentials/Secrets im Working-Tree.
      trufflehog ist nur ein Beispiel; jeder Scanner mit exit-1-on-find tut's."
    cmd: |
      python3 .peers/checks/scan_secrets.py .
    pass_when: "exit_code == 0"

  - id: deps-justified
    type: hard
    description: "Jede neu hinzugefügte runtime-dependency hat eine
      `Dependency-Justification:`-Note in einem Bug-Report-Commit."
    cmd: |
      python3 .peers/checks/deps_justified.py .
    pass_when: "exit_code == 0"

  - id: api-stable
    type: hard
    description: "Die in SPEC.md gelistete öffentliche API ist unverändert
      ODER der Commit trägt explizit einen Breaking-API:-Trailer."
    cmd: |
      python3 .peers/checks/api_stable.py .
    pass_when: "exit_code == 0"

  - id: no-prior-regression
    type: hard
    description: "Kein Test der VOR diesem Audit grün war ist jetzt rot.
      (Verhindert dass ein Fix andere Features mitnimmt.)"
    cmd: |
      python3 .peers/checks/no_regression.py .
    pass_when: "exit_code == 0"

  - id: diff-size-per-resolve
    type: hard
    description: "Jeder Bug-Resolves-Commit ändert ≤ 200 Zeilen netto.
      Riesige bundled-fix-Commits sind unreviewbar."
    cmd: |
      python3 .peers/checks/diff_size_per_resolve.py .
    pass_when: "exit_code == 0"

  # ===== SOFT GOALS — peers reviewen sich gegenseitig =====

  - id: bug-hunt-round-1-deep
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 1
    prompt: |
      Round 1 deep audit. KEINE ABKÜRZUNGEN. Lies JEDE Datei in src/
      und tests/ vollständig durch — nicht skimmen.

      Suche nach (alle Kategorien, je 5+ Findings angestrebt):
        - Logik-Fehler (off-by-one, falsche Conditional-Reihenfolge)
        - Race conditions / TOCTOU
        - Error-Handling-Lücken (silently-swallowed Exceptions)
        - Resource-Leaks (file handles, sockets, subprocess, threads)
        - Unbounded growth (lists, dicts, caches)
        - Input-Validation an System-Boundaries fehlt
        - Sicherheitslücken (cmd injection, path traversal, SSRF, …)
        - API-Verträge die nicht eingehalten werden
        - Spec-Verstöße (lies SPEC.md erneut, prüfe jedes Feature)
        - Confabulation-Risiken: Code wo DU unsicher bist wie er
          aufgerufen wird oder welche Inputs er kriegt. Solche
          unsicheren Stellen MÜSSEN als `Bug-Report:investigate-<X>`
          (severity info) gefiled werden — niemals raten.

      File jeden Defekt als Bug-Report-Commit nach BUG_HUNT_BLOCK-Schema
      mit ehrlicher Severity. Severity-Inflation und -Deflation sind
      beide schädlich; begründe in `## Bug-Report` warum diese severity.

      RATEN IST EINE ABKÜRZUNG. Wenn du nicht 100% verstehst was
      passiert, file investigate-X statt einen falschen Bug-Report.

      "Nichts gefunden" ist eine inhaltlich gehaltvolle Aussage — nur
      reply mit {"pass": true, "notes": "round 1: N filed (M crit/high)"}
      wenn du wirklich JEDE Datei durch hast UND begründen kannst dass
      die offenen N nicht severity-falsch sind.

  - id: bug-hunt-round-2-cross-review
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 1
    prompt: |
      Round 2: lies den DIFF des anderen peers seit peers-baseline,
      Datei für Datei. Hat er einen Bug fix gemacht? Prüfe kritisch:
        - Adressiert der Fix die ROOT CAUSE oder nur das Symptom?
        - Reihenfolge: gibt es einen `Bug-Reproduce:`-Commit der VOR
          dem `Bug-Resolves:` landet (failing test first)? Ohne den
          ist es kein TDD-Fix.
        - Wurden Tests für den Fix geschrieben (happy + edge + sad)?
        - Erzeugt der Fix neue Probleme (Performance-Regression,
          Lesbarkeit, unklare Naming, neue Race conditions)?
        - Ist der Fix die kleinste mögliche Änderung, oder bundled-in-
          drive-by Refactor der nicht zum Bug gehört?
        - Hat der Fix vorher-grüne Tests rot gemacht? Wenn ja: Bug-Report.

      Bug-Resolves nur dann signen wenn du nach diesem Audit überzeugt
      bist. Sonst: neuen Bug-Report `## Bug-Report` filen der erklärt
      WIESO der Fix unzureichend ist (mit konkretem Edge-Case oder
      Test der fehlschlagen würde). Alternativ: wenn der Fix
      grundsätzlich falsch ist UND zu groß für diese Session zum
      Neumachen, ein `Bug-Defer:`-Commit mit ehrlicher Begründung.

      Reply {"pass": true, "notes": "round 2: F new / R confirmed / U unconvinced / D deferred"}.

  - id: bug-hunt-round-3-spec-conformance
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 2
    prompt: |
      Round 3 FINAL: lies SPEC.md absatzweise nochmal. Für JEDEN Satz
      der ein Verhalten zusichert: such die entsprechende Test-Datei
      und prüfe ob das Verhalten getestet ist. Wenn der Test fehlt:
      file einen Bug-Report `missing-test:<feature>` mit severity med,
      schreibe direkt im selben Commit einen happy + edge + sad Test
      und resolve ihn.

      KEINE Abkürzungen. Wenn du "das ist offensichtlich, braucht keinen
      Test" denkst — schreib trotzdem den Test.

      Reply {"pass": true, "notes": "round 3 done: N missing-tests added"}.

  - id: tests-3-class-review
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 2
    prompt: |
      Lies JEDEN neuen oder geänderten Test im aktuellen Audit. Für
      jeden Test verifiziere:
        - happy: nominal input, expected output
        - edge: boundary (empty, max, off-by-one, unicode, very long)
        - sad: invalid input, malformed data, exceptions, timeouts,
               disk-full, network-fail, partial-state-rollback

      Reject `assert True`. Reject "the function returns something".
      Reject Tests die nur die happy path covern und edge/sad weglassen.

      Bei jedem rejected Test: file Bug-Report `weak-test:<file>:<name>`
      mit konkretem Vorschlag was die fehlende Klasse von Tests ist.

      Reply {"pass": bool, "notes": "...", "weak": [list of test names]}.

  - id: critical-fix-review
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 3
    prompt: |
      Kritische Bewertung der bisher gelandeten Fixes. Für jeden Commit
      mit `Bug-Resolves:`-trailer prüfe:
        1. War die ursprüngliche Findings-Severity korrekt? Wurde ein
           crit-Bug als med gedowngraded oder umgekehrt?
        2. Wurde der Bug an der ROOT CAUSE gefixt, oder bei einem
           Symptom (z.B. catch-all Exception statt Bug-Fix in der
           aufrufenden Funktion)?
        3. Ehrliche Tests dabei?
        4. Hätte der Fix den Bug VOR Phase-3i-Fixes überhaupt gefunden?

      Wenn ein Fix unkritisch durchgewinkt aussieht, file dazu einen
      `weak-resolution:BUG-NNN`-Bug-Report. Sei explizit anti-cargo-cult.

      Reply {"pass": bool, "notes": "N resolves audited, M flagged"}.

  - id: honesty-self-check
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Selbst-Audit, ehrlich. Beantworte JEDE Frage:
        - Hast du in dieser Audit-Runde mindestens einen Bug NICHT
          gefiled weil du dachtest "ach das ist zu klein"? Wenn ja:
          file ihn JETZT mit severity info.
        - Hast du einen Test "vereinfacht" weil edge-cases unbequem
          waren? Wenn ja: ergänze die fehlenden Cases JETZT.
        - Hast du einen Fix per `git revert` weggemacht weil ein
          Test fehlschlug, statt den Test zu fixen?
        - Gibt es Code den du _vermutet_ hast statt verifiziert?
          Wenn ja: nenne ihn beim Namen + füg eine Spec-conformance-
          Verifikation in den nächsten Tick ein.
        - Hast du eine neue dependency hinzugefügt (pip/npm/cargo)?
          Wenn ja: gibt es einen `Dependency-Justification:`-Trailer
          mit Begründung? Sonst JETZT nachreichen.
        - Hast du irgendwo geraten welche externe API existiert? Hast
          du sie konsultiert? Wenn unsicher: investigate-Bug filen.

      Reply {"pass": true, "notes": "honest list: ..."} — sei konkret.
      Pauschal-"ja, alles ehrlich" wird vom anderen peer rejected.

  - id: perf-no-regression
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Performance-Pass. Schau in SPEC.md unter "Performance-Hotpaths"
      und führe deren Benchmark JETZT aus (z.B. `python -m mything
      --bench=1000`). Vergleiche mit dem letzten in `.peers/perf.log`
      eingetragenen Wert (Format: ISO-ts | sha | hotpath | metric).
      Wenn keine perf.log existiert: lege sie an und schreibe den
      ersten Baseline-Eintrag.

      Falls die aktuelle Messung > 20% schlechter ist als die
      letzte: file Bug-Report `perf-regression:<hotpath>` mit
      severity med (oder höher wenn der hotpath user-facing ist) und
      konkretem before/after-Wert im `## Bug-Report` JSON-Block.

      Reply {"pass": bool, "notes": "perf: hotpath=<v> (baseline=<v>, delta=<%>)"}.

  - id: api-stability-check
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 3
    prompt: |
      Public-API-Stabilität. SPEC.md listet die öffentliche API
      (Funktionen/Klassen/CLI-Flags). Generiere einen API-Snapshot
      via `python3 .peers/checks/api_stable.py --dump > /tmp/api.now`
      und diff gegen `.peers/api-baseline.txt`.

      Jede Änderung an dieser Liste ist verdächtig: peers neigen zu
      drive-by Refactor. Für jede Änderung:
        - Wenn intentional + spec-konform: Commit muss
          `Breaking-API: <funcname>: <wie genau>` als Trailer haben
          UND einen Migrations-Hinweis im `## Bug-Resolution`.
        - Sonst: revert oder Bug-Report `unintended-api-break:<symbol>`.

      Reply {"pass": bool, "notes": "api: N added / M removed / K signature-changed"}.

  - id: defer-discipline
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Sichtung aller `Bug-Defer:`-Commits. Für jeden defer prüfe:
        - Gibt es eine `reason`/`note` im `## Bug-Defer` JSON-Block?
          (Ohne Begründung war's keine ehrliche defer-Entscheidung.)
        - Ist der defer-Grund plausibel ("zu groß"/"braucht neue
          dependency"/"braucht produktions-daten zum reproduzieren")
          oder offensichtlich ein "ich hab keine Lust"?
        - Ist im defer-Commit ein Next-Step-Hinweis für die nächste
          Session formuliert (welche Vorbereitung würde den Fix
          möglich machen)?

      Wenn ein defer fragwürdig ist: file Bug-Report `weak-defer:BUG-NNN`
      mit konkretem Vorschlag wie der Bug doch noch dieser Session
      angefasst werden könnte.

      Reply {"pass": bool, "notes": "defers reviewed: N total, M flagged"}.

  - id: docs-sync
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 4
    prompt: |
      Doc-Drift-Check. Für jeden Bug-Resolves-Commit prüfe:
        - Wurde das Verhalten in einer User-facing Datei beschrieben
          (README.md, docs/, docstring auf der public Funktion)?
        - Wenn ja: ist die Beschreibung noch korrekt nach dem Fix?
          Falls falsch: in DIESEM Audit nachziehen (eigener Commit
          mit `Bug-Resolves:` ist ok wenn Dokumentation der Bug war,
          sonst regulärer docs-update-Commit).
        - Gibt es eine CHANGELOG.md? Wenn ja: ist der Fix dort
          eingetragen?

      Reply {"pass": bool, "notes": "docs: N updates needed, M done"}.
```

Wenn du `.peers/goals.yaml` nach dem Editieren manuell übernimmst,
aktualisiere den Bestätigungs-Hash:

```sh
python3 - <<'PY'
import hashlib
from pathlib import Path

p = Path(".peers")
(p / "goals.sha256").write_text(
    hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n"
)
PY
```

Während eines laufenden `peers-ctl start` ist `goals.yaml` bewusst
geschützt: Änderungen oder eine Löschung lösen einen Halt mit klarer
Reason aus.

### Warum diese Goals so geschrieben sind

| Goal | Was es verhindert |
|------|------------------|
| `tests-cover-happy-edge-sad` als HARD | "complete" mit nur happy-Tests pro src-Datei |
| `bug-hunt-clean` als HARD | 0 offene crit/high/med ist nicht-verhandelbar |
| `tdd-reproduces-bug` als HARD | Tests die nach dem Fix dazugebaut wurden (passen nur zum Fix, nicht zum Bug) |
| `no-secrets-committed` als HARD | versehentliche commits von `.env`, credentials, tokens |
| `deps-justified` als HARD | drive-by `pip install foo` ohne Begründung |
| `api-stable` als HARD | unangekündigte breaking changes an der public API |
| `no-prior-regression` als HARD | Fix für Bug X bricht Feature Y still |
| `diff-size-per-resolve` als HARD | unreviewbare 800-Zeilen-bundled-Commits |
| `round-2-cross-review` + "TDD-order erzwingen" | Test-mit-Fix statt Test-vor-Fix; gegenseitiges Durchwinken |
| `critical-fix-review` separat | root-cause vs. symptom, severity-re-triage |
| `perf-no-regression` | O(n²)-Fix der alle Tests besteht aber 10× langsamer ist |
| `defer-discipline` | `Bug-Defer:` ohne Rationale wird gefangen |
| `docs-sync` | README/docstring/CHANGELOG-drift gegenüber gefixtem Verhalten |
| `honesty-self-check` | Selbst-Audit über Abkürzungen, Confabulation, ungerechtfertigte deps |

### Bug-Hunt-Trailers — Schnellübersicht

| Trailer | Bedeutung | Status für Gate |
|---------|-----------|----------------|
| `Bug-Report: BUG-NNN` | Finding gefiled | bug ist OFFEN |
| `Bug-Resolves: BUG-NNN` + JSON `"status":"fixed"` | Fix gelandet | bug ist GESCHLOSSEN |
| `Bug-Resolves: BUG-NNN` + `"status":"wontfix"` | Bewusst nicht gefixt | bug bleibt OFFEN (human muss explizit re-triagen) |
| `Bug-Defer: BUG-NNN` + `## Bug-Defer {reason}` | Zu groß für diese Session, dokumentiert | bug ist GESCHLOSSEN (für gate) + sichtbar in summary |
| `Bug-Reproduce: BUG-NNN` | Commit fügt failing test für den Bug hinzu | wird von `gate-tdd` ausgewertet |
| `Dependency-Justification: <package>: <why>` | Neue dep mit Grund | von `deps-justified` Check geprüft |
| `Breaking-API: <symbol>: <how>` | Intentionale API-Änderung | von `api-stable` als legitim akzeptiert |

`gate-tdd` wertet die Git-History linear aus. Wenn du Merge-Commits
oder Side-Branches nutzt, achte darauf, dass der `Bug-Reproduce`-Commit
semantisch vor dem zugehörigen `Bug-Resolves` landet; sonst kann ein
historisch später gemergter Reproduce wie ein fehlender TDD-Beleg wirken.

---

## 3) Check-Skripte (Referenz für `.peers/checks/`)

Mit `peers-ctl new --modes=audit` werden diese 6 Skripte automatisch
nach `.peers/checks/` kopiert und `goals.yaml` wird direkt darauf
verdrahtet. Die folgenden Bodies bleiben als Referenz und zum
Customizing hier; fuer JavaScript/TypeScript kannst du stattdessen
`--lang=js`, fuer Rust `--lang=rust`, fuer Go `--lang=go` verwenden.
Unbekannte Sprachen fallen bewusst auf Python zurueck.

### `coverage_3class.py`

```python
#!/usr/bin/env python3
"""Exit 1 if any non-trivial src/ file lacks happy + edge + sad tests."""
import re, sys
from pathlib import Path

KIND_RE = {
    "happy": re.compile(r"(?i)(happy|ok|success|nominal|baseline)"),
    "edge":  re.compile(r"(?i)(edge|boundary|empty|max|min|long|unicode)"),
    "sad":   re.compile(r"(?i)(sad|fail|error|invalid|exception|timeout|broken)"),
}

def kinds_in(testfile: Path) -> set[str]:
    text = testfile.read_text(errors="ignore")
    return {k for k, rx in KIND_RE.items()
            for m in re.finditer(r"def\s+(test_\w+)", text)
            if rx.search(m.group(1))}

def main(srcdir="src", testdir="tests"):
    missing: list[str] = []
    for src in Path(srcdir).rglob("*.py"):
        if src.name.startswith("_") or src.name == "__init__.py":
            continue
        if sum(1 for _ in src.open()) < 50:                # tiny module
            continue
        stem = src.stem
        candidates = list(Path(testdir).rglob(f"test_{stem}*.py"))
        if not candidates:
            missing.append(f"{src}: no test_{stem}* anywhere in {testdir}/")
            continue
        kinds = set().union(*(kinds_in(c) for c in candidates))
        gap = {"happy", "edge", "sad"} - kinds
        if gap:
            missing.append(f"{src}: missing {sorted(gap)} test class(es)")
    if missing:
        print("coverage_3class FAIL:\n  " + "\n  ".join(missing))
        return 1
    print(f"coverage_3class: clean ({srcdir} ↔ {testdir})")
    return 0

if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3] if len(sys.argv) >= 3 else ()))
```

### `scan_secrets.py`

Das Template scannt Git-tracked Dateien plus untracked, nicht ignorierte
Dateien (`git ls-files --cached --others --exclude-standard`). Absichtlich
ignorierte Dateien wie `.env` bleiben Git-Policy; wenn du sie trotzdem
auditieren willst, nimm einen echten Filesystem-Scanner wie trufflehog in
deinen Projekt-Gates dazu.

```python
#!/usr/bin/env python3
"""Exit 1 if a credential-shaped string sneaks into the working tree.

Production-grade option: shell out to `trufflehog filesystem .` or
`git-secrets --scan`. Below is a minimal stdlib version that catches
the most common patterns (good enough as a first gate; supplement
with a real scanner for SOC2-grade audits)."""
import re, subprocess, sys
from pathlib import Path

PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"),            "AWS access key id"),
    (re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{6,}"), "hard-coded password"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][a-z0-9_\-]{16,}"), "API key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"),        "GitHub PAT"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"),         "OpenAI-style secret"),
]
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules",
        ".venv", "venv", ".peers"}

def main(root: str = "."):
    findings = []
    tree = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True
    )
    files = [Path(root) / f for f in tree.stdout.splitlines() if f]
    for f in files:
        if any(s in f.parts for s in SKIP):
            continue
        try:
            text = f.read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for rx, label in PATTERNS:
            for m in rx.finditer(text):
                findings.append(f"{f}:{text[:m.start()].count(chr(10))+1}: {label}")
    if findings:
        print("secrets FAIL:\n  " + "\n  ".join(findings))
        return 1
    print(f"secrets: clean ({len(files)} files scanned)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `deps_justified.py`

```python
#!/usr/bin/env python3
"""Exit 1 if a runtime dependency line was added without an explicit
Dependency-Justification: trailer in any commit that touched the
dependency file. Adjust DEP_FILES to your stack."""
import subprocess, sys
from pathlib import Path

DEP_FILES = ["pyproject.toml", "requirements.txt", "package.json",
             "Cargo.toml", "go.mod"]

def changed_dep_lines(repo: str) -> list[str]:
    # Compare HEAD against peers-baseline (or initial commit fallback).
    base = subprocess.run(
        ["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True
    ).stdout.strip().splitlines()[-1]
    baseline_ref = "peers-baseline" if subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "peers-baseline"],
        capture_output=True
    ).returncode == 0 else base
    out = subprocess.run(
        ["git", "-C", repo, "diff", f"{baseline_ref}..HEAD", "--unified=0",
         "--", *DEP_FILES],
        capture_output=True, text=True
    ).stdout
    added: list[str] = []
    for line in out.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
    return [a for a in added if a and not a.startswith("#")]

def has_justifications(repo: str) -> set[str]:
    log = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD",
         "--grep=^Dependency-Justification:", "--format=%B"],
        capture_output=True, text=True
    ).stdout
    out = set()
    for line in log.splitlines():
        if line.startswith("Dependency-Justification:"):
            pkg = line.split(":", 2)[1].strip().split()[0]
            out.add(pkg.lower().rstrip(","))
    return out

def main(repo: str = "."):
    added = changed_dep_lines(repo)
    justified = has_justifications(repo)
    missing = []
    for line in added:
        pkg = line.split()[0].split("=")[0].split(">")[0].split("<")[0]\
                  .strip("\"',").lower()
        if pkg and pkg not in justified:
            missing.append(f"{pkg}  (added in deps but no Dependency-Justification: trailer)")
    if missing:
        print("deps_justified FAIL:\n  " + "\n  ".join(missing))
        return 1
    print(f"deps_justified: clean ({len(added)} added, all justified)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `api_stable.py`

```python
#!/usr/bin/env python3
"""Snapshot the public API surface; fail if it drifted without an
explicit Breaking-API: trailer in the corresponding commit.

Usage:
  api_stable.py --dump > .peers/api-baseline.txt   # einmal beim Audit-Start
  api_stable.py                                    # gate-Modus
"""
import ast, subprocess, sys
from pathlib import Path

def public_symbols(srcdir: str = "src") -> list[str]:
    out = []
    for f in sorted(Path(srcdir).rglob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        mod = ".".join(f.with_suffix("").parts[1:])
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    out.append(f"{mod}.{node.name}")
    return out

def main():
    if "--dump" in sys.argv:
        for s in public_symbols():
            print(s)
        return 0
    baseline = Path(".peers/api-baseline.txt")
    if not baseline.exists():
        print("api_stable: no baseline — run with --dump first to capture")
        return 1
    declared = set(baseline.read_text().splitlines())
    actual = set(public_symbols())
    added = actual - declared
    removed = declared - actual
    log = subprocess.run(
        ["git", "log", "peers-baseline..HEAD", "--grep=^Breaking-API:",
         "--format=%B"],
        capture_output=True, text=True
    ).stdout
    allowed = {line.split(":", 1)[1].strip().split(":")[0].strip()
               for line in log.splitlines()
               if line.startswith("Breaking-API:")}
    unannounced = (added | removed) - allowed
    if unannounced:
        print("api_stable FAIL: unannounced API changes:")
        for s in sorted(unannounced):
            kind = "added" if s in added else "removed"
            print(f"  {kind}: {s}")
        return 1
    print(f"api_stable: clean (+{len(added)}/-{len(removed)} all "
          f"covered by Breaking-API: trailers)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### `no_regression.py`

```python
#!/usr/bin/env python3
"""Exit 1 if a test that was green at peers-baseline is now red.

Records the set of passing test nodeids at baseline (one-time) into
.peers/passing-baseline.txt via pytest's --junitxml output. On
gate-run, runs the suite NOW and compares: any baseline-passing test
that isn't in the new passing set is a regression."""
import subprocess, sys, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

BASELINE = Path(".peers/passing-baseline.txt")

def collect_passing() -> set[str]:
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    subprocess.run(
        ["python3", "-m", "pytest", "-q", "--no-header",
         f"--junitxml={xml_path}", "--tb=no"],
        capture_output=True, text=True
    )
    tree = ET.parse(xml_path)
    Path(xml_path).unlink(missing_ok=True)
    out: set[str] = set()
    for tc in tree.getroot().iter("testcase"):
        # Failed/errored tests carry <failure>/<error> children; passing
        # ones have neither (skipped → <skipped/>, treat as not-passing).
        if any(child.tag in ("failure", "error", "skipped") for child in tc):
            continue
        cls = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        out.add(f"{cls}::{name}" if cls else name)
    return out

def main():
    if "--snapshot" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        passing = collect_passing()
        BASELINE.write_text("\n".join(sorted(passing)) + "\n")
        print(f"no_regression: snapshot saved to {BASELINE} ({len(passing)} tests)")
        return 0
    if not BASELINE.exists():
        print(f"no_regression: missing {BASELINE}; "
              "run once with --snapshot at audit start")
        return 1
    expected = set(BASELINE.read_text().splitlines()) - {""}
    actual = collect_passing()
    regressed = expected - actual
    if regressed:
        print(f"no_regression FAIL: {len(regressed)} previously-green tests are red:")
        for t in sorted(regressed)[:30]:
            print(f"  {t}")
        return 1
    print(f"no_regression: clean ({len(expected)} baseline-green still green)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Note: uses pytest's `--junitxml` output rather than parsing stdout —
robust gegen Format-Änderungen in pytest's terminal-reporter (z.B.
zwischen `-q`/`-v`/colored). Das Python-Template ist pytest-spezifisch;
die `--lang=js|rust|go` Scaffold-Varianten legen stack-spezifische
`no_regression.sh` Einstiege an, die du bei Bedarf gegen Jest-, Cargo-
oder Go-JSON-Reporter haerter machen kannst.

### `diff_size_per_resolve.py`

```python
#!/usr/bin/env python3
"""Exit 1 if any Bug-Resolves commit exceeds NET_LINES of diff."""
import subprocess, sys

LIMIT = 200
def main(repo: str = "."):
    log = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD",
         "--grep=^Bug-Resolves:", "--format=%H"],
        capture_output=True, text=True
    ).stdout.splitlines()
    over: list[str] = []
    for sha in log:
        ns = subprocess.run(
            ["git", "-C", repo, "show", "--stat", "--format=", sha],
            capture_output=True, text=True
        ).stdout.strip().splitlines()
        if not ns:
            continue
        last = ns[-1]
        # "N files changed, X insertions(+), Y deletions(-)"
        ins = del_ = 0
        for tok in last.split(","):
            tok = tok.strip()
            if "insertion" in tok:
                ins = int(tok.split()[0])
            elif "deletion" in tok:
                del_ = int(tok.split()[0])
        net = ins + del_
        if net > LIMIT:
            over.append(f"  {sha[:8]}: {net} lines (limit {LIMIT})")
    if over:
        print("diff_size_per_resolve FAIL:\n" + "\n".join(over))
        return 1
    print(f"diff_size_per_resolve: clean ({len(log)} resolves, all ≤ {LIMIT})")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `verify_self_review.py` (für `self-review-on-handoff`)

Der Default nutzt den vertrauenswürdigen Package-Checker:

```sh
python3 -m peers.templates.modes.audit.checks.verify_self_review
```

`peers init` kopiert zusätzlich eine kompatible Datei nach
`.peers/checks/verify_self_review.py`, damit bestehende Projekte und
lokale Spezial-Checks weiter funktionieren. Für neue Goals ist der
Package-Pfad robuster, weil er nicht von einem editierbaren Target-Repo
abhängt.

---

## 3.5) Modes stacken — wenn du mehr willst als nur Bug-Audit

`--modes` ist eine komma-separierte Liste. Built-in:
- `audit` — Bug-Audit (alles aus §3)
- `thorough` — Anti-Convergence-Theater: HARD-gate auf N=3 aufeinanderfolgende saubere Ticks + Skeptic-Pass + Aggressive-Honesty
- `describe` — peers schreiben SPEC/ARCH/DESIGN-Docs, nicht audit
- `implement` — Feature-Implementierung aus PLAN.md (standalone, nicht stackable)

```sh
peers-ctl new myapp --modes=audit,thorough --spec ./myapp-spec.md
```

Eigene scopes (z.B. `security-crypto`, `security-mobile`) gehen via
user-mode unter `~/.config/peers/modes/<name>/`. Siehe `peers-ctl modes list`.

### Externe Tools als User-Modes

`~/.config/peers/modes/cloc-baseline/`:
- `mode.yaml`: `{name: cloc-baseline, version: 1, description: ...}`
- `goals.yaml`: ein `cmd:` der das externe Binary aufruft (z.B. `cloc`).

Dann:
```sh
peers-ctl new myapp --modes=audit,security,cloc-baseline
```

`peers-ctl modes list` zeigt alle verfügbaren Modes (built-in + user).

### Tiefes Audit: `--modes=audit,security,thorough`

Wenn du wirklich "läuft bis nichts mehr da" willst:

```sh
peers-ctl new myapp --modes=audit,security,thorough --spec ./spec.md
```

Was `thorough` zusätzlich bringt:
- **HARD `convergence-reached`**: braucht N=3 (default, override via
  `goals.convergence_n` in config.yaml) aufeinanderfolgende Ticks ohne
  neue crit/high/med-Bug-Reports + ohne neue weak-fix/shallow-fix
  Flag-Bugs aus dem security-mode. Info-Findings zählen nicht für
  reset — sonst läuft die Loop ewig auf "info: missing docstring".
- **SOFT `skeptic-pass`** alle Ticks: peers müssen pro Datei 5
  Failure-Modes konkret begründen + ausschließen, sonst rejected.
- **SOFT `aggressive-honesty`** alle 3 Ticks: pro Top-Level-Pfad
  müssen peers 3 Failure-Modes + 2 Security-Kategorien + 1
  coverage-loch konkret nennen.

Empfohlene config.yaml-Anpassungen wenn du thorough stackst:

```yaml
budget:
  max_iterations: 500       # thorough braucht 20-50 mehr Ticks
  max_runtime_s: 86400      # 24h Notbremse
  max_consecutive_failures: 10
goals:
  convergence_n: 3          # 3 saubere ticks; auf 5 für noch strenger
```

---

## 4) Einmal-Setup beim Audit-Start

`peers init` (via `peers-ctl new`) hat bereits den Tag `peers-baseline`
auf HEAD gesetzt — der ist die Anker-Referenz für alle Check-Skripte
die mit `peers-baseline..HEAD` arbeiten.

Zusätzlich vor dem ersten `peers-ctl start`:

```sh
cd ~/c0de/peers-c0de/myapp

# Audit-env einfrieren (für Reproduzierbarkeit)
{
  echo "audit-started: $(date -Is)"
  echo "peers: $(peers --version)"
  echo "peers-ctl: $(peers-ctl --version)"
  echo "claude: $(claude --version 2>/dev/null || echo n/a)"
  echo "codex: $(codex --version 2>/dev/null || echo n/a)"
  echo "podman: $(podman --version)"
  echo "git: $(git rev-parse HEAD)"
} > .peers/audit-env.txt

# Snapshots für no_regression + api_stable
python3 .peers/checks/no_regression.py --snapshot
python3 .peers/checks/api_stable.py --dump > .peers/api-baseline.txt

git add .peers/audit-env.txt .peers/passing-baseline.txt .peers/api-baseline.txt
git commit -m "audit: capture env + baseline snapshots"
git tag -f peers-baseline HEAD     # bewege den anker auf DIESEN Commit
```

Der letzte `git tag -f` verschiebt `peers-baseline` so dass die
baseline-snapshots SELBST nicht als "Audit-Diff" gezählt werden.

---

## 5) config.yaml — Audit-tauglich

```yaml
driver: orchestrator
comm: hybrid                          # peers reden auch über files, nicht nur git

peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "json", "{PROMPT}"]   # json → USD-Tracking
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv: ["codex", "exec",
           "--skip-git-repo-check",
           "--sandbox", "workspace-write",
           "--dangerously-bypass-approvals-and-sandbox", "{PROMPT}"]
    prompt_mode: argv-substitute

budget:
  max_iterations: 50                  # Audit braucht viele Ticks
  max_runtime_s: 28800                # 8 h Notbremse
  max_consecutive_failures: 5
  max_usd_mode: auto                  # OAuth-Setup → warn (kein hard kill)

health:
  idle_timeout_s: 1800                # 30 min — peers denken lange auf großen Repos
  absolute_max_runtime_s: 7200
  error_patterns:
    # Defaults aus template/config.yaml übernehmen (ERROR/FATAL-anchored)

# Bei pytest >120s hier hochsetzen
goals:
  timeout_s: 600                      # 10 min — reicht für die meisten Suiten
```

---

## 6) Starten + Mitlesen

```sh
PEERS_CTL_PODMAN_NETWORK=host \
    peers-ctl start myapp --container --max-ticks 50 --max-usd 100

# Drei Terminals zum Mitlesen (alternativ tmux):
peers-ctl tail myapp                  # Container-Log live
peers-ctl status myapp                # current goal status + peer health
watch -n 30 'python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp'
```

Was du in einer "echten" Audit-Session zu sehen kriegst:
- Tick 1–3: peers lesen den ganzen src/-Tree, filen erste Welle Bugs
- Tick 4–8: Round-2-cross-review läuft, eine Hälfte der Round-1-Findings
  wird re-triaged oder als "weak resolution" markiert
- Tick 9–15: Code-Fixes landen, Tests werden geschrieben
- Tick 16+: round-3-spec-conformance findet missing-tests; honesty-self-check
  triggert weitere kleine Findings
- Convergence: bug-hunt-clean exit 0 + alle hard goals pass + soft consensus ≥2/2

---

## 7) Manuelles Stoppen wenn nötig

```sh
peers-ctl stop myapp                  # SIGTERM → 10s grace → SIGKILL
```

Substrate persistiert State sauber via SIGTERM-Handler. Du kannst später
`peers-ctl start myapp --container --max-ticks 50` machen und es
läuft weiter: `state.json` wird atomar geschrieben, und `goals.yaml`
wird gegen den Start-Snapshot geschützt. Wenn du Goals ändern willst,
stoppe den Lauf, passe `goals.yaml` an, aktualisiere `goals.sha256`
und starte neu.

---

## 8) Abnahme — keine Abkürzungen

```sh
# Volle Re-Validierung aller harden Gates
peers -C ~/c0de/peers-c0de/myapp verify
cat ~/c0de/peers-c0de/myapp/.peers/VERIFY.md

# Bug-Bilanz: was wurde gefiled, was resolved, was deferred
python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp

# TDD-Disziplin: jeder blocking fix hatte einen failing test ZUERST?
python3 -m peers.bug_hunt gate-tdd ~/c0de/peers-c0de/myapp

# Lies REPORT.md — die Substrate-eigene Zusammenfassung
cat ~/c0de/peers-c0de/myapp/.peers/REPORT.md

# Lies runs.jsonl — Tick-für-Tick was passierte
jq -s '.' ~/c0de/peers-c0de/myapp/.peers/log/runs.jsonl | less
```

**Kritische Betrachtung der Findings — pflicht:**

1. **Severity-Sanity-Check.** Geh durch `bug_hunt summary` und frag bei
   jedem crit/high: "würde ein erfahrener Engineer das wirklich so
   einstufen?". peers neigen zu Severity-Inflation am Anfang und
   -Deflation am Ende. Bei Diskrepanz: manuell prüfen, ggf. einen
   weiteren Tick mit re-triage-Prompt anwerfen.

2. **Fix-Quality-Stichproben.** Wähle 5 zufällige `Bug-Resolves:`-Commits.
   Lies ihren Diff. Frag dich: hätte ICH den Bug so gefixt? Wenn nicht,
   schau ob es einen guten Grund gibt oder ob das Cargo-Cult ist.

3. **Test-Quality-Stichproben.** Wähle 5 zufällige neue Test-Funktionen.
   Lies sie. Sind happy + edge + sad wirklich abgedeckt, oder hat
   der peer drei Test-Funktionen mit demselben happy-case geschrieben
   und die Klasse "edge" / "sad" nur über den Namen suggeriert?

4. **Spec-Conformance-Stichprobe.** Lies SPEC.md absatzweise. Pro
   zugesichertem Verhalten: existiert ein Test? Wenn nein, ist
   round-3 falsch durchgelaufen — neuer Tick.

5. **Ehrlichkeitscheck.** Lies die Antworten der `honesty-self-check`
   Reviews. "Alles gut, nichts gefunden" über mehrere Runs in Folge
   ist verdächtig.

---

## 9) Commiten + Pushen

Das Substrate committet schon während der Loop laufend, jeder Commit
trägt `Peer: <name>` + Self-Review-Trailer. Du musst aber nochmal
nachvollziehen + auf den eigenen Branch pushen:

```sh
cd ~/c0de/peers-c0de/myapp

# Was hat der Audit geändert?
git log --oneline peers-baseline..HEAD | head -50
git diff --stat peers-baseline..HEAD

# Letzter Sanity-Check vor push — JEDE Zeile muss exit 0
python3 -m pytest -q                                # tests grün?
ruff check .                                        # lint clean?
python3 -m peers.bug_hunt gate .                    # 0 crit/high/med?
python3 -m peers.bug_hunt gate-tdd .                # TDD-disziplin?
python3 .peers/checks/scan_secrets.py .             # keine secrets?
python3 .peers/checks/deps_justified.py .           # neue deps gerechtfertigt?
python3 .peers/checks/api_stable.py .               # API-stabil?
python3 .peers/checks/no_regression.py .            # keine vorher-grünen rot?
python3 .peers/checks/diff_size_per_resolve.py .    # alle resolves ≤ 200 LOC?
peers verify                                        # alle hard goals grün?

# Wenn ALLES grün:
git remote -v                                       # auf welche origin pushst du?
git push origin <dein-branch>
```

Wenn dein Workflow Pull-Requests vorsieht:

```sh
gh pr create --title "Audit + fix run on myapp" --body "$(cat <<'EOF'
## Summary
- Vollständiger peers-Audit + Fix (claude + codex, $N Ticks, $M USD)
- $K Bug-Reports gefiled, $J resolved (severity-Verteilung im Body)
- Tests: $B → $A passing (+$delta neue Tests)
- Lint/type: clean

## Bug-Bilanz
$(python3 -m peers.bug_hunt summary . | head -40)

## Test plan
- [ ] `pytest -q` lokal grün auf clean clone
- [ ] `peers verify` exit 0
- [ ] Manual smoke-test: <project-specific>
- [ ] Spec-conformance-Spot-Check auf 3 zufälligen SPEC-Sätzen

🤖 Generated by peers-substrate via Claude Code
EOF
)"
```

**Bevor du den PR mergst:** lies die Bug-Bilanz selbst, nicht nur die
Zusammenfassung. Verifiziere stichprobenartig. Wenn was nicht passt:
neuen Audit-Tick mit gezieltem prompt drauf, statt zu mergen und später
zu fixen.

---

## 10) Häufige Stolperfallen

- **`/tmp` ist tmpfs**: Audit-Projekte gehören NICHT nach `/tmp/`. Nutz
  `$PEERS_PROJECTS_ROOT` (default `~/c0de/peers-c0de/`) — sonst gehen
  Projekte nach einem Reboot verloren.

- **PID-1-Annahmen in deinen Tests:** falls deine Test-Suite irgendwo
  `os.kill(1, …)` oder `os.killpg(0, …)` macht in der Annahme PID 1 sei
  init/systemd: im peers:dev-Container ist PID 1 das Substrat (uid 1000).
  Mocken statt echt killen.

- **idle_timeout_s zu klein**: häufigster Failure-Modus. claude `-p` ist
  während der Arbeit komplett still (kein streaming). Faustregel:
  600 s nur für kleine Fixes; 1800–3600 s für Multi-File; 3600+ s für
  große Audits.

- **pasta-Network-Bug** auf manchen Hosts: `PEERS_CTL_PODMAN_NETWORK=host`
  vor `peers-ctl start ...`.

- **Goal-Mutation-Halt**: ein peer hat `goals.yaml` editiert oder
  gelöscht. Loop hält an — by design. Stop, manuell entscheiden ob du
  das übernehmen willst, `goals.sha256` aktualisieren, neu starten.
  Der Start-Snapshot schützt den laufenden Durchgang auch dann, wenn
  jemand `goals.yaml` und `goals.sha256` zusammen verändert.

- **api-error in runs.jsonl**: loggt `matched_error_pattern` +
  `stderr_tail`. Damit findest du heraus ob echt rate-limit oder
  config-issue (z.B. fehlende `--dangerously-bypass-approvals-and-sandbox`
  bei codex).

- **Convergence dauert lang**: Audit eines 5k-LOC-Projekts braucht
  realistisch 20–40 Ticks à ~5–15 min = 2–10 h Wallclock + $30–100 USD
  bei API-Billing (OAuth: gratis). Plane das ein, oder zieh den
  `max_iterations` runter wenn du nur einen Quick-Pass willst.

---

## 11) Ehrlichkeit — Meta-Reminder

Diese Anleitung garantiert keinen perfekten Audit. Was sie garantiert:

- **Zwei unabhängige peer-Augen** auf jeden Bug-Report (`reviewer: both`,
  `consensus_needed: 2`)
- **Strukturell verhindertes "complete"** ohne 0-crit/high/med
- **Strukturell erzwungenes** happy/edge/sad-test-class-coverage
- **Selbst-Audit** ("honesty-self-check") als wiederkehrender Prompt
- **Mensch im Loop** für severity-sanity, fix-quality, spec-conformance

Was sie NICHT ersetzt:

- Deine eigene kritische Lektüre der Findings + Fixes
- Dein Domänen-Wissen über die App
- Penetration-Testing für sicherheitskritische Apps (peers finden
  klassische OWASP-Patterns, aber kein State-of-the-Art-Exploit-Chaining)
- Performance-Profiling (peers reviewen Code, nicht Latency-Profile)

Wenn der Audit "alles grün" sagt und dein Bauchgefühl sagt "das war zu
schnell" — vertraue dem Bauchgefühl. Neuer Tick mit gezieltem
Skeptiker-Prompt:

```yaml
- id: skeptic-pass
  type: soft
  reviewer: both
  consensus_needed: 2
  review_interval: 1
  prompt: |
    Der vorige Audit kam zu "alles grün". Das ist verdächtig.
    Geh JEDES src-File nochmal durch und finde mindestens 1 Bug
    den die vorigen Rounds übersehen haben. Wenn du nach
    gewissenhafter Suche wirklich nichts findest, dokumentiere
    KONKRET welche 5 Failure-Modes du geprüft + ausgeschlossen
    hast. Pauschal-"sauber" wird vom anderen peer rejected.
```

---

## TL;DR (für die Eile-Variante)

```sh
peers-ctl new myapp --container --modes=audit --spec ./spec.md
cd ~/c0de/peers-c0de/myapp
$EDITOR .peers/{goals,config}.yaml SPEC.md          # goals/config/SPEC trimmen
python3 .peers/checks/no_regression.py --snapshot
python3 .peers/checks/api_stable.py --dump > .peers/api-baseline.txt
git add .peers && git commit -m "audit: baseline" && git tag -f peers-baseline
cd -

PEERS_CTL_PODMAN_NETWORK=host \
    peers-ctl start myapp --container --max-ticks 50 --max-usd 100
peers-ctl tail myapp                  # zuschauen
# warten bis "complete" oder manueller stop

peers -C ~/c0de/peers-c0de/myapp verify
python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp
python3 -m peers.bug_hunt gate-tdd ~/c0de/peers-c0de/myapp
# kritisch lesen, stichproben, ggf. nochmal mit skeptic-pass laufen
git push origin <branch>
```
