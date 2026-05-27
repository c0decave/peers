# peers-ctl modes — inspect available audit modes

## NAME
peers-ctl modes — list or show audit/security/thorough modes that
`peers init --modes=...` (and `peers-ctl new --modes=...`) can pick up.

## SYNOPSIS
```
peers-ctl modes list
peers-ctl modes show <name>
```

## DESCRIPTION
`modes list` runs `peers.modes.discover()` and prints a table:

```
NAME              VER    SOURCE    DESCRIPTION
audit             v3     builtin   Default audit gates (...)
security          v1     builtin   OWASP-driven security review
my-custom-mode    v0     user      ...
```

`modes show <name>` dumps the named mode's `mode.yaml`, its
`goals.yaml`, and a listing of its `checks/` directory. Use it to
sanity-check what `--modes=<name>` would copy into `.peers/`.

User modes (under `$PEERS_MODES_DIR` or
`~/.config/peers/modes/<name>/`) shadow built-in modes by name —
the SOURCE column tells you which copy wins.

## OPTIONS
- `list` — no options.
- `show <name>` — required positional: mode name.

## EXAMPLES
```
peers-ctl modes list
peers-ctl modes show audit
peers-ctl modes show security | less
```

## FILES
- Reads: bundled modes under `peers/templates/modes/`,
  `$PEERS_MODES_DIR/<name>/`, `~/.config/peers/modes/<name>/`.

## ENVIRONMENT
- `PEERS_MODES_DIR` — extra discovery directory (in addition to
  bundled + `~/.config/peers/modes/`).

## SEE ALSO
- `peers init --help-man` — `--modes` flag uses this discovery.
- `peers-ctl new --help-man` — same `--modes` flag.

## NOTES
- `show` reads from inside the package OR `$PEERS_MODES_DIR`, both
  trusted sources, so it uses plain `read_text()` rather than the
  hardened no-symlink path.
- Unknown mode names exit 1 with the list of available names.
