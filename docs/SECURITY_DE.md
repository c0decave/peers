# peers — Security & Threat Model

**Sprachen:** [EN](SECURITY.md) / DE

**Status:** lebendes Dokument. Das Threat Model und die mitigierenden
Maßnahmen pro Schicht beschreiben den aktuellen Codezustand. Offene
Lücken sind ausdrücklich benannt, damit Operatoren entscheiden können,
ob ein Deployment in ihren Scope passt.

## TL;DR

`peers` steuert nicht vertrauenswürdige LLM-CLIs (`claude`, `codex`)
gegen ein Source-Repository, das sie ändern dürfen. Die Security-Haltung
ist **Single-Operator auf einem vertrauenswürdigen Host**, nicht
Multi-Tenant. Der peers-Container ist eine Defense-in-Depth-Grenze, kein
harter Sandbox-Service für beliebigen Drittcode.

Für bezahlte Kunden- oder Shared-Host-Deployments: vor dem Shippen den
Abschnitt **Bekannte Lücken** lesen.

## Vertrauensgrenzen

```
[ Host-Operator ] ─── vertraut ──→ [ peers-ctl auf dem Host ]
                                             │
                                             │ startet
                                             ▼
                                [ peers-egress-proxy Sidecar ]
                                    (Allow-List für LLM-Hosts)
                                             ▲
                                             │ HTTPS via 127.0.0.1:3128
                                             │
                                [ peers-auth-proxy Sidecar ]
                                    (Claude OAuth, falls vorhanden)
                                             ▲
                                             │ HTTP via 127.0.0.1:8080
                                             │
                                [ peers main container ]
                                             │
                                             │ stdin/stdout/argv
                                             ▼
                              [ claude / codex CLIs im Container ]
                                             │
                                             │ Shell-Tool-Subprocess
                                             ▼
                                [ /work Bind-Mount (Target Repo) ]
```

| Schicht | Vertrauen | Heutige Verteidigung |
|---|---|---|
| Host-Operator | voll | — |
| `peers-ctl` auf dem Host | vertrauenswürdig | `_PROJECT_NAME_RE`, `safe_io` (`O_NOFOLLOW` + nlink), `0o700` Config-Dirs, `fcntl.flock`, atomic rename + dir-fsync |
| Egress-Proxy-Sidecar | vertrauenswürdig | `cap-drop=ALL`, `no-new-privileges`, `--read-only`, `--tmpfs`, feste Allow-List, `pids-limit=128`, keine Host-Bind-Mounts |
| Auth-Proxy-Sidecar | vertrauenswürdig | `cap-drop=ALL`, `no-new-privileges`, `--read-only`, `/tmp` + `/auth` tmpfs, `pids-limit=128`, OAuth-Injektion + Refresh |
| peers-Container | semi-vertrauenswürdig | `cap-drop=ALL`, `no-new-privileges`, `userns=keep-id`, `pids-limit=8192`, `--read-only`, `--tmpfs`, Netzwerk über Sidecars, HealthGuard, Zombie-Reaper |
| claude / codex CLIs | **nicht vertrauenswürdig** | `--dangerously-bypass-approvals-and-sandbox`, weil die äußere Sandbox die Grenze ist |
| `/work` Bind-Mount | nicht vertrauenswürdiger Inhalt | read-write für Commits; Tamper-Detection, `goals.sha256`, Anti-Cheating-Revert pro Tick |

## Threat Actors

1. **Prompt Injection über Repo-Inhalt** — Ziel-Docs, Source-Kommentare
   oder Tool-Beschreibungen fordern das LLM auf, Secrets zu exfiltrieren.
2. **Mode-Config-Tampering** — `.peers/goals.yaml` enthält Shell-Gates.
   Ein kompromittierter Peer könnte die Goals ändern; `goals.sha256`
   und `goal-mutation` stoppen das.
3. **Peer-Trailer-Spoofing** — ein Peer commitet mit gefälschtem
   `Peer: <other>` Trailer. Autor-/Committer-Checks fangen das ab.
4. **Prompt-Leak über `/proc/cmdline`** — `prompt_mode:
   argv-substitute` macht Prompts für `ps` sichtbar. Bekannte Lücke.
5. **Container Escape via CLI-Sandbox-Bypass** — `codex
   --dangerously-bypass-approvals-and-sandbox` ist aktiv. Der
   peers-Container ist die Sandbox-Grenze; mitigiert, aber nicht
   unfehlbar.
6. **Concurrent `peers-ctl start`** — Start-Lock reduziert TOCTOU; die
   Registry bleibt hostseitig single-user.
7. **Disk Exhaustion** — keine vollständige ENOSPC-Recovery; tmp-Dateien
   können liegen bleiben.
8. **Host-Reboot mitten im Run** — Reconcile markiert `crashed` oder
   `unknown`; Operator entscheidet über Restart.

## Mitigations pro Schicht

### peers-ctl (Host)

- **`safe_io.py`** — Reads/Writes nutzen `O_NOFOLLOW`, Post-Open
  `(dev,ino)`-Check, `nlink != 1`-Refusal und Mode-Re-Narrowing.
- **Projektname-Regex** `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` blockt
  `..`, `/`, NUL und Shell-Metazeichen.
- **Registry-Lock** via `fcntl.flock` auf `~/.config/peers-ctl/.lock`.
- **Atomare State-Writes** — `state.json.tmp` → `rename` + `fsync(dir)`.

### Egress-Proxy-Sidecar

- **Hard ACL** in `proxy/filter-allow.txt`; alles andere bekommt 403.
- **`FilterDefaultDeny Yes`** — Allow-List, keine Blocklist.
- **Loopback-only Listener**, kein breites Bind-Interface.
- **`--read-only` rootfs**, nur `/tmp`, `/run/tinyproxy` und
  `/var/log/tinyproxy` als tmpfs beschreibbar.
- **Keine Host-Mounts** — der Sidecar kann keine Host-Dateien lesen.
- **Netzwerk-Umkehrung** — der peers-Container teilt die Namespace des
  Proxys, nicht umgekehrt.

### Auth-Proxy-Sidecar

- **Claude-OAuth-Isolation** — wenn `~/.claude.json` existiert, mountet
  `peers-ctl start --container` sie nur in
  `peers-auth-proxy_<project>`.
- **Workspace-Route** — Workspace erhält
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`; der Sidecar setzt
  `Authorization: Bearer ...`.
- **Refresh on 401** — Token wird aus der gemounteten Datei erneuert und
  die API-Anfrage einmal wiederholt. Endpoint kommt aus
  `AUTH_PROXY_OAUTH_TOKEN_URL` oder `tokenUrl`. Der Endpoint muss eine
  `https://`-URL sein, damit der `refresh_token` nie im Klartext an einen
  Off-Box-Host geht; `http://` ist nur für die Loopback-Hosts `127.0.0.1`,
  `::1` und `localhost` erlaubt (RFC 8252 §7.3) — die verlassen die
  Maschine nie und erlauben lokalen Dev/Integration-Tests den Flow ohne
  TLS. Eine fehlerhafte URL wird gleich behandelt (strukturierter Fehler,
  kein Crash).
- **Read-only rootfs mit expliziten Schreibpfaden** — nur `/tmp` und
  `/auth` sind als tmpfs beschreibbar. Token-Refresh nutzt normal
  atomic replace; Datei-Bindmounts fallen auf fsync'd In-place-Rewrite
  zurück.
- **Netzwerkplatzierung** — mit Egress-Proxy teilen Auth-Proxy und
  Workspace dessen Namespace; ohne Egress teilt der Workspace die
  Auth-Proxy-Namespace.

### peers main container

- **`cap-drop=ALL + no-new-privileges`** — keine privileged Ops, keine
  suid-Eskalation.
- **`userns` mit dem netns-Owner geteilt** — die rootless-Mapping (UID 1000 innen = 1000 außen, `keep-id`) wird vom **egress-Proxy** (dem Owner des network namespace) erzeugt. Auth-Proxy und Main-Container joinen ihn via `--userns=container:<egress-proxy>`, sodass alle drei *einen* user namespace teilen. Das ist zwingend: Der Kernel erlaubt das Mounten eines frischen `sysfs` auf `/sys` nur, wenn der eigene user namespace den geteilten network namespace *besitzt* — ein Main-Container mit eigenem `keep-id`-userns scheiterte daher an `runc create` mit `mounting sysfs to /sys: operation not permitted` (rc=126). In den Bypass-/Host-Net-Modi besitzt der Container seinen eigenen netns und behält ein selbst erzeugtes `keep-id`.
- **`--read-only` rootfs** — keine Persistenz in `/usr`, `/etc`, `/var`.
- **Explizite `--tmpfs`** für `/tmp`, `~/.cache`,
  `~/.npm`, jeweils `nosuid,nodev`.
- **Netzwerk via Egress-Proxy** — `--network=container:peers-egress-proxy_<project>`
  plus `HTTPS_PROXY`.
- **Kein `~/.claude.json` im Workspace bei aktivem Auth-Proxy** —
  Claude-Creds liegen im Sidecar; `~/.claude/` bleibt für Session-jsonl.
- **`pids-limit=8192`** gegen Prozess-Stürme.

### LLM-CLI-Schicht

- **`claude -p --dangerously-skip-permissions`** akzeptiert die äußere
  Container-Grenze.
- **`codex exec --dangerously-bypass-approvals-and-sandbox`** ebenso;
  ohne das blockiert die interne Sandbox legitime `.git`-Writes.
- **HealthGuard** mit Idle- und Absolute-Timeout.
- **Zombie-Reaper im Container**, weil `peers` PID 1 ist.

## Erforderliche Outbound-Domains

Kanonische Basis-Allowlist ist
[`proxy/filter-allow.txt`](../proxy/filter-allow.txt). Projektbezogene
Runtime-Erweiterungen, aktuell OpenRouter, werden via
`PEERS_EGRESS_EXTRA_HOSTS` injiziert und nicht vom Host gemountet.

### Erforderlich

| Host | Genutzt von | Zweck | Multi-Tenant? |
|---|---|---|---|
| `api.anthropic.com` | claude CLI / Auth-Proxy | Anthropic Messages API | Nein |
| `chatgpt.com` + `*.chatgpt.com` | codex CLI | Subscription/WebSocket/OAuth Flow | kontrolliert durch OpenAI-Account |
| `api.openai.com` | codex CLI | API-Key-Pfad | Nein |

### Runtime-Erweiterungen (nur bei opt-in-Projekten)

| Host | Trigger | Zweck | Multi-Tenant? |
|---|---|---|---|
| `openrouter.ai` | Peer mit `provider: openrouter` | OpenRouter-Gateway für Claude-/Codex-kompatible Modelle | Kein user-kontrolliertes Subdomain-Wildcard; exakter Host |

### Spekulativ / aktuell nicht erlaubt

| Host | Warum entfernt |
|---|---|
| `statsig.anthropic.com` | im Proxy-Traffic nie beobachtet |
| `claude.ai` + `*.claude.ai` | nie benötigt; Anthropic-owned, aber nicht global nötig |

### Explizit nicht erlaubt

| Host | Warum geblockt |
|---|---|
| `github.com` / `api.github.com` | nicht nötig für Audit-Arbeit; wäre generische Exfil-Route |
| `pypi.org` | Supply-Chain-/Exfil-Risiko |
| `http-intake.logs.us5.datadoghq.com` | Third-Party-Telemetrie, multi-tenant |
| `*.ingest.sentry.io` | Wildcard auf attacker-registrierbare Org-Hosts wäre C2-fähig |
| `featuregates.org`, `statsigapi.net`, `events.statsigapi.net` | Multi-Tenant-Telemetrie |
| `prodregistryv2.org` | npm/yarn Registry, nicht nötig |

### Warum keine Wildcards auf Third-Party-Telemetrie?

Ein Pattern wie `.*\.sentry\.io$` matcht beliebige
`<orgname>.ingest.sentry.io`. Da Sentry, Datadog und Statsig öffentliche
Multi-Tenant-Dienste sind, könnte ein Angreifer eine eigene Org anlegen
und den Allow-List-Regex als Exfil-Kanal nutzen. Deshalb nur konkrete
Hosts unter vendor-kontrollierten Domains erlauben.

## Bekannte Lücken

Nicht für Third-Party-Kunden shippen, bevor diese Punkte bewusst
akzeptiert oder geschlossen sind:

1. **Nicht-Claude-Credentials** — `~/.codex` und `~/.claude/` bleiben
   read-write gemountet. Claude-root-Auth (`~/.claude.json`) ist im
   Auth-Proxy isoliert; Codex-Creds hängen weiter am Egress-Proxy und
   Single-Operator-Trust.
2. **argv-substitute leakt Prompts** — Fix-Design: `prompt_mode:
   argv-substitute` → stdin.
3. **Kein Seccomp-Profil** — `cap-drop=ALL` ist gut, aber kein
   Kernel-Attack-Surface-Narrowing.
4. **Keine echte Multi-Tenant-Isolation** — Registry ist single-user.
5. **Keine Resource Quotas** — `--memory`, `--cpus` fehlen.
6. **Disk-full-Recovery fehlt** — ENOSPC ist noch Operator-Arbeit.
7. **Supply Chain nicht gepinnt** — npm/pip Installationen sind nicht
   hash-gepinnt.
8. **Hosts ohne `/dev/net/tun`** — bei `PEERS_CTL_EGRESS_PROXY_NETWORK=host`
   ist die Isolation schwächer; der Proxy bleibt für gutartige Clients
   wirksam, aber rohe Sockets können den Proxy umgehen.

## Operator-Playbook

### Proxy-Härtung aktivieren

```sh
make build
make proxy-build
make auth-proxy-build
peers-ctl new <name> --container --modes=...
peers-ctl start <name> --container
```

### Härtung prüfen

```sh
podman ps --filter name=peers-egress-proxy_<name>
podman ps --filter name=peers-auth-proxy_<name>
podman inspect peers-ctl_<name> | grep NetworkMode
podman inspect peers-ctl_<name> | grep claude.json
```

Erwartung: Main-Container hängt an `container:peers-egress-proxy_<name>`;
`~/.claude.json` ist nicht im Workspace gemountet, sondern im
Auth-Proxy unter `/auth/.claude.json`.

### Budget Controls (`peers-ctl start`)

| Flag | Wirkung |
|---|---|
| `--max-runtime DURATION` | überschreibt `budget.max_runtime_s` vor dem Start; akzeptiert `300s`, `90m`, `6h`, `2d`, `1w` |
| `--reset-budget` | setzt spent-Counter auf 0, erhält aber Tick-Historie |
| `--force` | überspringt den Budget-Preflight; Run schreibt den Sentinel trotzdem |

### Escape Hatches

| Var | Wirkung | Einsatz |
|---|---|---|
| `PEERS_CTL_NO_EGRESS_PROXY=1` | Egress-Sidecar aus, Legacy-Netzwerk | Debugging, nicht produktiv |
| `PEERS_CTL_NO_AUTH_PROXY=1` | Auth-Sidecar aus, Legacy-`~/.claude.json` Workspace-Mount | Auth-Debugging, nicht multi-tenant |
| `PEERS_CTL_PODMAN_NETWORK=<mode>` | Main-Container-Netzwerk im Legacy-Pfad | Rootless-Networking-Probleme |
| `PEERS_CTL_EGRESS_PROXY_NETWORK=<mode>` | Netzwerk des Egress-Proxys | private Alternative validieren |
| `PEERS_CTL_EGRESS_PROXY_IMAGE=<tag>` | anderes Egress-Proxy-Image | reproduzierbarer Pin |
| `PEERS_CTL_AUTH_PROXY_IMAGE=<tag>` | anderes Auth-Proxy-Image | reproduzierbarer Pin |
| `AUTH_PROXY_OAUTH_TOKEN_URL=<url>` | Refresh-Endpoint überschreiben | Tests oder Tokenfiles ohne `tokenUrl` |

### Allow-List erweitern

1. Projekt mit `make proxy-build` starten.
2. Logs lesen: `podman logs -f peers-egress-proxy_<name>`.
3. `Filter denied` Hosts prüfen.
4. Anchored Regex in `proxy/filter-allow.txt` ergänzen.
5. `make proxy-build`, dann `peers-ctl stop` + `peers-ctl start`.

**Regel:** nur LLM-API-Hosts und minimale Telemetrie zulassen. Keine
generischen CDN- oder Third-Party-Wildcards.

### Disaster: ein Peer hat Tokens geleakt

1. Sofort OAuth/API Keys bei Anthropic/OpenAI rotieren.
2. Proxy-Logs auf unerwartete Ziele prüfen.
3. Alle Container via `peers-ctl stop` stoppen.
4. `goals.yaml` der letzten Projekte auf unautorisierte Gate-Kommandos
   prüfen; `goal-mutation` sollte so etwas gestoppt haben.
