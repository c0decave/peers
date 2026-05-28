# `implement`-Mode — Feature-Implementierung aus PLAN.md

**Sprachen:** [EN](MODES_IMPLEMENT.md) / DE

End-to-end Feature-Implementierung aus einer Markdown-`PLAN.md`, mit
Reviewer-only Check-offs, eingefrorenen Acceptance-Verträgen,
Blind-Review zwischen Peers, verpflichtender Pessimismus-Quote,
finalem `HONESTY_AUDIT.md` und Cleanliness-Gates, die Shortcut-Marker,
leere Implementierungen und geskipte Tests bei Convergence verweigern.
In v1 standalone — **nicht kombinierbar** mit
`audit`/`security`/`thorough`.

---

## TL;DR

```sh
peers-ctl new myfeature --container --modes=implement --plan ./PLAN.md
peers-ctl start myfeature --container --max-runtime 12h
peers-ctl tail myfeature
peers-ctl ack-block myfeature STEP-7 --reason "API key not provisioned"
peers-ctl amend  myfeature --acceptance "pytest -k new_path" \
                            --reason "scope expanded; old acceptance subsumed"
```

`--plan FILE` ist mit `--modes=implement` **pflicht** und gegenseitig
exklusiv zu `--spec`. Der Plan wird validiert, die deklarierte
Acceptance-Command wird als Preflight ausgeführt (sie MUSS aktuell
fehlschlagen — sonst ist die Arbeit schon erledigt), und `PLAN.md`,
`acceptance.sh` sowie optional `e2e.sh` werden unter `.peers/` mit
sha256-Pins eingefroren.

---

## PLAN.md Schema-Referenz

`PLAN.md` ist ein kleines line-orientiertes Markdown-Dialekt, geparst
von `src/peers_ctl/plan_parser.py`. Der Parser ist strikt: Ambiguität
führt zu `PlanValidationError` bei `peers-ctl new`, bevor ein Tick
läuft.

Pflichtsektionen: `## Meta` und `## Steps`. Optional:
`## Architecture`, `## Input Domains`.

### Vollständiges Beispiel

```markdown
# Feature: user login with refresh tokens

## Meta
surfaces: [cli, web]
acceptance: pytest tests/acceptance/test_login.py
e2e: playwright test e2e/login.spec.ts
convergence_n: 5
mutation_testing: false
honesty_audit_peer: gemini

## Architecture
- AuthMiddleware: session cookie + refresh-token rotation
- SessionStore: redis-backed, 24h TTL, per-user revocation
- LoginRoute: POST /api/login, returns session + refresh in body

## Input Domains
- username: ascii [a-z0-9_-]{3,32}
- password: utf-8, 8..256 chars
- refresh_token: opaque uuid4

## Steps
- [ ] [STEP-1] Add SessionStore (in-memory backend)
  - touches: src/session/store.py, tests/test_session_store.py
  - rationale: STEP-2 + STEP-3 both depend on it
- [ ] [STEP-2] Add AuthMiddleware
  - touches: src/middleware/auth.py, tests/test_auth_middleware.py
  - depends: [STEP-1]
- [ ] [STEP-3] Add POST /api/login route
  - touches: src/routes/login.py, tests/test_login_route.py
  - depends: [STEP-1, STEP-2]
- [ ] [STEP-4] Wire e2e happy-path
  - touches: e2e/login.spec.ts
  - depends: [STEP-3]
  - trivial_step: true
```

### Meta Keys

| Key | Pflicht | Typ | Notizen |
|---|---|---|---|
| `surfaces` | ja | `[a, b, c]` Liste | `web` oder `gui` erzwingt `e2e:` |
| `acceptance` | ja | Shell-Command | wird als `.peers/contracts/acceptance.sh` eingefroren |
| `e2e` | bedingt | Shell-Command | Pflicht, wenn `surfaces` `web`/`gui` enthält |
| `convergence_n` | nein (Default 5) | int | N saubere Ticks bis Convergence |
| `mutation_testing` | nein (Default false) | bool | aktiviert `mutation-sample` Soft-Gate |
| `honesty_audit_peer` | nein | Peer-Name | erzwingt 3rd-Peer in `HONESTY_AUDIT.md` |
| `confidence_calibration` | nein (Default false) | bool | aktiviert pro Step `confidence: N/5` |

### Step Lines

```text
- [<MARK>] [STEP-N] <free text>
  - touches: file1, file2, ...
  - depends: [STEP-X, STEP-Y]
  - rationale: <prose>
  - trivial_step: true
  - pure_refactor: true
  - confidence: 4/5
```

`<MARK>` ist einer dieser Marker:

| Marker | Bedeutung |
|---|---|
| `[ ]` | offen |
| `[x]` / `[X]` | erledigt; braucht trailing `(SHA)` und Check-off durch den anderen Peer |
| `[PARTIAL]` | in Arbeit; blockiert `plan-checklist-empty` |
| `[BLOCKED]` | externer Blocker; blockiert `no-unresolved-blocks` |
| `[BLOCKED-ACK]` | Operator hat Blocker via `peers-ctl ack-block` anerkannt |

`[STEP-N]` IDs müssen **1-indexed und sequenziell** sein. `depends:`
muss auf deklarierte Steps zeigen und darf keinen Zyklus bilden. Ein
erledigter Step darf eine SHA-Annotation tragen, z.B.
`[x] [STEP-2] AuthMiddleware (a1b2c3d)`. `plan-step-traceable` prüft
SHA und bei `touches:` auch die geänderten Dateien.

---

## Worked Example — kleines Feature von Anfang bis Ende

**Ziel:** `--count` Flag für ein `wordcount` CLI-Tool.

### 1. PLAN.md schreiben

```markdown
# Feature: --count flag for wordcount

## Meta
surfaces: [cli]
acceptance: pytest tests/test_count_flag.py -v

## Steps
- [ ] [STEP-1] Parse --count from argv
  - touches: src/wordcount/cli.py, tests/test_count_flag.py
- [ ] [STEP-2] Wire counter into output path
  - touches: src/wordcount/core.py, tests/test_count_flag.py
  - depends: [STEP-1]
- [ ] [STEP-3] Document --count in README
  - touches: README.md
  - depends: [STEP-2]
  - trivial_step: true
```

### 2. Scaffold + Start

```sh
peers-ctl new wordcount-count --container \
  --modes=implement --plan ./wordcount-count-plan.md

peers-ctl start wordcount-count --container --max-runtime 6h
peers-ctl tail wordcount-count
```

`new` validiert den Plan, führt die Acceptance-Command aus (muss
fehlschlagen), friert `acceptance.sh` + `PLAN.original.md` ein und
registriert das Projekt.

### 3. Was die Loop tut

- **Tick 0 — recon** schreibt `RECON.md`.
- **Tick 1 — alignment** schreibt `PLAN.aligned.md`.
- **Tick 2 — architecture intent** schreibt
  `ARCHITECTURE.intended.md`; der Substrat pinnt den SHA.
- **Ticks 3+ — implementer / reviewer alternation:**
  - Implementer commitet Code + `IMPLEMENTATION_NOTES.md`.
  - Reviewer liest nur den Diff, schreibt `REVIEW_NOTES.md`, vergleicht
    dann mit `IMPLEMENTATION_NOTES.md` und setzt passende Steps auf
    `[x] (sha)`.
  - Divergenz landet als `[BLIND-REVIEW-MISMATCH]` in `CONCERNS.md`.
  - `pre-commit-reviewer-checkoff` verhindert Self-Checkoff;
    `checkoff-by-other-peer` ist das nachgelagerte Gate.
- **Convergence Tick** schreibt `HONESTY_AUDIT.md`; alle Hard Gates
  müssen grün sein, danach verlangt `delivery-report-complete` ein
  `DELIVERY.md`, das jeden `STEP-N` auf Commit, Tests und Begründung
  mappt.

### 4. DELIVERY.md

```markdown
# Delivery — wordcount --count flag

## [STEP-1] Parse --count from argv
- **Commit:** a1b2c3d
- **Tests:** tests/test_count_flag.py::test_count_short, ::test_count_long
- **Justification:** argparse subparser; happy/edge/sad covered.

## [STEP-2] Wire counter into output path
- **Commit:** d4e5f6a
- **Tests:** tests/test_count_flag.py::test_count_changes_output
- **Justification:** reviewer confirmed both formats.

## [STEP-3] Document --count in README
- **Commit:** 7890abc
- **Tests:** N/A
- **Justification:** docs-only diff; trivial_step: true documented.
```

Erlaubte Commit-Werte: hex SHA, `PENDING` oder `BLOCKED`.
`plan-step-traceable` validiert SHA separat; dieses Gate erzwingt nur
die Struktur.

---

## Honesty-Mechanismen

Die zentrale Wette des Modus: **zwei Peers, die denselben Diff
unabhängig lesen, finden mehr als ein Peer, der seine eigene Arbeit
rationalisiert**.

1. **Frozen Contracts** (`src/peers_ctl/contracts.py`) — `PLAN.md` wird
   nach `.peers/PLAN.original.md` gesnapshottet, `acceptance.sh` und
   optional `e2e.sh` werden eingefroren und per sha256 gepinnt.
   `contracts-unchanged` prüft jeden Tick. Legitime Änderungen laufen
   über `peers-ctl amend` und landen hash-chained in
   `.peers/contracts.log`.
2. **Blind Review** (`blind-review` Gate) — Implementer schreibt
   `IMPLEMENTATION_NOTES.md`; Reviewer liest den **Diff allein** und
   schreibt `REVIEW_NOTES.md`. Divergenz wird in `CONCERNS.md`
   gemeldet.
3. **Reviewer-only Checkoffs** — Hook + Gate verhindern, dass derselbe
   Peer Code schreibt und die eigene Checkbox flippt.
4. **Mandatory Pessimism** — `CONCERNS.md` darf im Convergence-Modus
   nicht leer bleiben; null Bedenken über mehrere Ticks ist
   Rubber-Stamping, nicht Perfektion.
5. **HONESTY_AUDIT** — jeder Peer schreibt H3-Sektionen zu schwächstem
   Teil, wahrscheinlich unentdecktem Bug und Shortcut-Risiko.

Dazu kommen **Cleanliness Gates**: `no-shortcut-markers`,
`no-empty-bodies`, `no-skipped-tests`. Legitime Escapes brauchen eine
Code-Annotation (`# JUSTIFIED:` / `# SKIP-REASON:`) und einen
reviewer-signierten Eintrag in `.peers/justifications.log`.

---

## Escape-Valves Cookbook

### `[PARTIAL]` — ein Step ist noch in-flight

```markdown
- [PARTIAL] [STEP-2] AuthMiddleware
  - touches: src/middleware/auth.py
  - rationale: rotation logic landed; revocation pending in next tick
```

`plan-checklist-empty` bleibt rot, bis der Marker auf `[x]` oder
`[BLOCKED]` wechselt. Sparsam nutzen; gute Steps passen in einen Tick.

### `[BLOCKED]` — externer Blocker

```markdown
- [BLOCKED] [STEP-7] Wire OAuth callback
  - touches: src/auth/oauth.py
  - rationale: requires GOOGLE_OAUTH_CLIENT_ID secret; not in env
```

Operator-Aktion:

```sh
peers-ctl ack-block myfeature STEP-7 \
  --reason "OAuth integration deferred to next sprint; mock backend covers happy path"
```

`ack-block` validiert den Marker, schreibt `[BLOCKED-ACK]` und hängt
einen hash-chained Eintrag an `.peers/blocks.log`.

### `peers-ctl amend` — legitime Acceptance-Änderung

```sh
peers-ctl amend myfeature \
  --acceptance "pytest tests/acceptance/ -v" \
  --reason "scope expanded to include refresh-token rotation; old single-file acceptance subsumed"
```

Das ersetzt `.peers/contracts/acceptance.sh`, aktualisiert den Pin und
protokolliert die Änderung in `.peers/contracts.log`.

### `# JUSTIFIED:` + signierter Eintrag

Für bewusst belassene Shortcut-Marker:

```python
def stub_for_now() -> None:  # JUSTIFIED: blocked on STEP-7 OAuth wiring
    raise NotImplementedError
```

Dazu ein reviewer-signierter Eintrag in `.peers/justifications.log`.
`no-shortcut-markers` akzeptiert nur, wenn beide Hälften vorhanden sind.

### `# SKIP-REASON:`

Für geskipte Tests gilt dasselbe Zwei-Schlüssel-Prinzip:

```python
# SKIP-REASON: requires GPU runner; covered by tests/integration/test_gpu.py
@pytest.mark.skipif(not has_gpu(), reason="needs CUDA")
def test_kernel_fusion() -> None:
    ...
```

---

## FAQ

### Wann `implement`, wann `audit`?

| Situation | Mode |
|---|---|
| Greenfield Feature mit klarer PLAN.md | **`implement`** |
| Existierende Codebase härten | `audit` (+`thorough`, +`security-*`) |
| Existierende Codebase ohne Docs | erst `describe`, dann `audit` |
| Implementieren und danach auditieren | `implement` convergen lassen, danach neu mit `--modes=audit,thorough --force` |

`implement` ist in v1 nicht mit Audit/Security/Thorough stackbar; Tick
Rhythmus und Gate-Set sind anders.

### Budget-Tipps

- Default `max_runtime_s` ist **12h**.
- Per `peers-ctl start ... --max-runtime 24h` für eine Session erhöhen.
- `--reset-budget` setzt spent-Counter zurück, erhält aber die
  Tick-Nummerierung.
- Phase 0 (Ticks 0-2) schreibt Recon/Alignment/Architecture, also
  genügend Slack vor dem Coding einplanen.

### Sprach-Support

Die Implement-Templates sind Python-lastig:
`coverage_3class_delta` nutzt Python-Begriffe, `no-empty-bodies` läuft
über Python-AST, `no-shortcut-markers` scannt `src/`. Strukturelle Gates
wie `plan-checklist-empty`, `contracts-unchanged`,
`delivery-report-complete`, `blind-review`, `concerns-resolved`,
`honesty-audit` und `no-unresolved-blocks` sind sprachagnostisch.
Für JS/Rust/Go vor produktiver Nutzung passende Check-Varianten planen.

### Was, wenn ein Peer cheatet?

- Implementer checkt eigene Steps ab → Hook blockt; Gate fängt Altfälle.
- Acceptance wird trivialisiert → `contracts-unchanged` schlägt an.
- Shortcut-Marker, leere Bodies oder geskipte Tests bleiben übrig →
  Cleanliness-Gates blocken Convergence.
- Reviewer rubber-stamped mit leerem `CONCERNS.md` →
  `concerns-resolved` schlägt an.
- Boilerplate in `HONESTY_AUDIT.md` → Struktur-Gate fordert substantielle
  H3-Antworten.

### Was, wenn die Loop nie converged?

| Symptom | Wahrscheinliche Ursache | Fix |
|---|---|---|
| `plan-checklist-empty` läuft stundenlang rot | Step zu groß | in kleinere `STEP-N`s splitten |
| `blind-review` scheitert wiederholt | Reviewer liest falsche Artefakte | Prompt erneut lesen, ggf. stoppen + manuell resetten |
| `acceptance-pass` flaky | Test hängt an Environment | Env in `acceptance.sh` pinnen via `peers-ctl amend` |
| `coverage-3class-delta` stuck | Edge/Sad Tests fehlen | pro Step Tests ergänzen |
| Budget erschöpft | Feature zu groß | `--reset-budget --max-runtime 24h` oder Plan teilen |

### Wo leben die Gates?

Implement-mode-spezifische Gates liegen unter
`src/peers/templates/modes/implement/checks/X.py` und werden via
`python3 -m peers.cli run-check <name>` entdeckt.

---

## Siehe auch

- [`docs/plans/2026-05-26-implement-mode-design.md`](plans/2026-05-26-implement-mode-design.md)
- [`docs/plans/2026-05-26-implement-mode-implementation.md`](plans/2026-05-26-implement-mode-implementation.md)
- [`src/peers/templates/modes/implement/goals.yaml`](../src/peers/templates/modes/implement/goals.yaml)
- [`src/peers_ctl/plan_parser.py`](../src/peers_ctl/plan_parser.py)
- [`src/peers_ctl/contracts.py`](../src/peers_ctl/contracts.py)
- [`src/peers_ctl/justifications.py`](../src/peers_ctl/justifications.py)
- [`README_DE.md`](../README_DE.md) — deutscher Überblick
