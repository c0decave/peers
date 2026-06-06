"""CODEMAP — the machine-readable source of truth for `document` mode.

A CODEMAP.yaml lists one entry per documented symbol. The drift gates
(`grounded`, `signature-match`, `complete`) verify each entry against the
real code via the AST, so the docs cannot drift from / hallucinate the
codebase. This module holds the parser + the AST analysis the thin gate
scripts call (see src/peers/templates/modes/document/checks/).
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_KINDS = {"module", "class", "function", "method"}
_REQUIRED = ("id", "kind", "file", "line")


class CodeMapError(Exception):
    """Raised when a CODEMAP.yaml is missing or structurally invalid."""


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    file: str
    line: int
    signature: str | None = None
    summary: str = ""

    @property
    def name(self) -> str:
        """The symbol's bare name — the last dotted segment of the id."""
        return self.id.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class CodeMap:
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class SymbolInfo:
    kind: str  # "function" | "class" | "method"
    lineno: int
    params: list[str]  # [] for classes


def _render_params(args: ast.arguments) -> list[str]:
    """Render a function's parameter NAMES in source order. `*args`/`**kw`
    keep their prefix; defaults/annotations are intentionally ignored (we
    compare names, not values). The bare `*` keyword-only separator is not
    emitted (it is not a parameter)."""
    out: list[str] = []
    out.extend(a.arg for a in args.posonlyargs)
    out.extend(a.arg for a in args.args)
    if args.vararg is not None:
        out.append("*" + args.vararg.arg)
    out.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg is not None:
        out.append("**" + args.kwarg.arg)
    return out


_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _expected_key(e: Entry) -> str:
    """The qualname `index_module` would use for this entry. For a method the
    enclosing class is the second-to-last dotted segment of the id."""
    if e.kind == "method":
        parts = e.id.split(".")
        cls = parts[-2] if len(parts) >= 2 else ""
        return f"{cls}.{e.name}"
    return e.name


def check_grounded(project_dir: Path, codemap: CodeMap) -> list[str]:
    """Return a list of ungrounded-entry messages (empty = clean).

    An entry is grounded when its file exists and — for class/function/method
    kinds — a symbol of that kind and name actually lives in that file. A
    `module` entry only requires the file to exist. This is the anti-
    fabrication core: a hallucinated or wrong-file symbol is reported.
    """
    project_dir = Path(project_dir)
    violations: list[str] = []
    for e in codemap.entries:
        fpath = project_dir / e.file
        if not fpath.is_file():
            violations.append(f"{e.id}: file not found: {e.file}")
            continue
        if e.kind == "module":
            continue
        idx = index_module(fpath)
        if idx is None:
            violations.append(f"{e.id}: cannot parse {e.file}")
            continue
        info = idx.get(_expected_key(e))
        if info is None or info.kind != e.kind:
            violations.append(
                f"{e.id}: no {e.kind} `{e.name}` found in {e.file}"
            )
    return violations


def _render_signature(name: str, params: list[str]) -> str:
    """`name(p1, p2, ...)` — re-parseable by `parse_signature_params`."""
    return f"{name}({', '.join(params)})"


def iter_public_entries(project_dir: Path) -> list[Entry]:
    """Every public symbol under `src/` as a structural `Entry`
    (id/kind/file/line/signature, no summary). The generative sibling of
    `enumerate_public_symbols`: their id-sets are identical by construction.
    Public = no dotted segment starts with `_`. Files that fail to parse are
    skipped (robust)."""
    project_dir = Path(project_dir)
    src = project_dir / "src"
    out: list[Entry] = []
    if not src.is_dir():
        return out
    for py in sorted(src.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        parts = list(py.relative_to(src).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        module_id = ".".join(parts)
        if not module_id:
            continue
        rel = py.relative_to(project_dir).as_posix()
        out.append(Entry(id=module_id, kind="module", file=rel, line=1))
        idx = index_module(py)
        if idx is None:
            continue
        for qual, info in sorted(idx.items()):
            if any(seg.startswith("_") for seg in qual.split(".")):
                continue
            name = qual.rsplit(".", 1)[-1]
            sig = (_render_signature(name, info.params)
                   if info.kind in ("function", "method") else None)
            out.append(Entry(id=f"{module_id}.{qual}", kind=info.kind,
                             file=rel, line=info.lineno, signature=sig))
    return out


def enumerate_public_symbols(project_dir: Path) -> set[str]:
    """The set of public symbol ids under `src/` (the surface the CODEMAP
    must cover). Public = name not starting with `_`. Includes each module id
    (dotted path from src/, `__init__` collapsed to the package), every public
    top-level class/function, and public methods of public classes. Delegates
    to `iter_public_entries` so the gate and the generator share one walk."""
    return {e.id for e in iter_public_entries(project_dir)}


def check_complete(project_dir: Path, codemap: CodeMap) -> list[str]:
    """Return messages for public symbols absent from the CODEMAP (empty =
    clean) — i.e. undocumented public surface."""
    documented = {e.id for e in codemap.entries}
    missing = enumerate_public_symbols(project_dir) - documented
    return [f"missing from CODEMAP: {sid}" for sid in sorted(missing)]


# Vacuous summaries that `complete`/`grounded` would pass but that document
# nothing. Compared case-insensitively against the stripped summary.
_PLACEHOLDER_SUMMARIES = frozenset(
    {"todo", "tbd", "fixme", "xxx", "...", "n/a", "na", "tk", "wip", "?"}
)
_MIN_SUMMARY_LEN = 3


def check_summaries(codemap: CodeMap) -> list[str]:
    """Return messages for CODEMAP entries lacking a substantive summary
    (empty = clean). The structural gates (`grounded`/`signature-match`/
    `complete`) prove the map points at real code; this gate proves the map
    actually *documents* it — an empty, whitespace, too-short, or placeholder
    summary is undocumented surface. The build-driving gate for `document`
    mode (a freshly seeded structural CODEMAP has no summaries → all red)."""
    violations: list[str] = []
    for e in codemap.entries:
        s = (e.summary or "").strip()
        if not s:
            violations.append(f"{e.id}: missing summary")
        elif s.lower() in _PLACEHOLDER_SUMMARIES or len(s) < _MIN_SUMMARY_LEN:
            violations.append(
                f"{e.id}: placeholder/too-short summary {e.summary!r}"
            )
    return violations


# --- ARCHITECTURE.md (Phase 3 human docs) — anchor resolution + coverage -----

ARCHITECTURE_FILE = "ARCHITECTURE.md"
# The seed writes this sentinel into each outline section; the gate fails while
# any remains, so a half-filled skeleton stays red (the prose-presence proxy).
ARCH_PLACEHOLDER = "<!-- TODO: write this section -->"

_ANCHOR_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _strip_code_fences(text: str) -> str:
    """Drop fenced code blocks (``` or ~~~) so a literal [[…]] inside an example
    does not register as an anchor. Inline code is left intact — an inline
    `[[id]]` is still a real anchor."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def parse_anchors(text: str) -> list[str]:
    """Every `[[id]]` anchor in `text`, in document order, fenced code blocks
    ignored. Ids are stripped; duplicates are preserved (callers dedupe)."""
    body = _strip_code_fences(text)
    return [m.group(1).strip() for m in _ANCHOR_RE.finditer(body)]


def _subsystem_of(entry_id: str) -> str | None:
    """The top-level subsystem an id belongs to — the PACKAGE-QUALIFIED top-level
    module (`peers.tick_loop.X` -> `peers.tick_loop`, `peers_ctl.store.Store` ->
    `peers_ctl.store`). None for a bare top-level package id. Qualifying by
    package keeps same-named modules in different packages distinct (e.g.
    `peers.cli` vs `peers_ctl.cli`) — `src/` can hold more than one package."""
    parts = entry_id.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


def required_subsystems(codemap: CodeMap) -> set[str]:
    """The public top-level subsystems the architecture narrative must cover:
    every package-qualified top-level module in the CODEMAP whose module name is
    not underscore-private."""
    subs: set[str] = set()
    for e in codemap.entries:
        parts = e.id.split(".")
        if len(parts) >= 2 and not parts[1].startswith("_"):
            subs.add(f"{parts[0]}.{parts[1]}")
    return subs


def check_architecture(project_dir: Path, codemap: CodeMap) -> list[str]:
    """Return violations for `<project_dir>/ARCHITECTURE.md` (empty = clean).
    The HARD, deterministic moat for the human-docs prose:
      - every `[[id]]` anchor resolves to a real CODEMAP entry (no dangling),
      - every public top-level subsystem is anchored >=1x (coverage),
      - no leftover seed placeholder remains (the prose-presence proxy).
    Whether the surrounding prose is *true* is the soft `architecture-cross-
    review`'s job — same division of labor as summaries-complete vs
    summaries-cross-review. A missing file = every subsystem uncovered (red)."""
    path = Path(project_dir) / ARCHITECTURE_FILE
    if not path.is_file():
        return [f"{ARCHITECTURE_FILE}: missing "
                "(a document-mode build seeds and fills it)"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{ARCHITECTURE_FILE}: unreadable: {e}"]
    violations: list[str] = []
    if ARCH_PLACEHOLDER in text:
        violations.append(
            f"{ARCHITECTURE_FILE}: leftover seed placeholder — replace every "
            "outline section with real prose")
    ids = {e.id for e in codemap.entries}
    covered: set[str] = set()
    for a in parse_anchors(text):
        if a not in ids:
            violations.append(
                f"{ARCHITECTURE_FILE}: dangling anchor [[{a}]] — "
                "not a CODEMAP entry")
            continue
        s = _subsystem_of(a)
        if s:
            covered.add(s)
    for s in sorted(required_subsystems(codemap) - covered):
        violations.append(
            f"{ARCHITECTURE_FILE}: subsystem not covered: {s} "
            "(anchor at least one of its symbols)")
    return violations


def parse_signature_params(sig: str) -> list[str] | None:
    """Param names from a documented signature string, via the SAME renderer
    as `index_module` (so they compare directly). Accepts `name(...)` or a
    bare `(...)`. Returns None when the string is not a valid signature."""
    s = sig.strip()
    for candidate in (f"def {s}: ...", f"def _s{s}: ..."):
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        fn = tree.body[0]
        if isinstance(fn, _FUNC):
            return _render_params(fn.args)
    return None


def check_signatures(project_dir: Path, codemap: CodeMap) -> list[str]:
    """Return signature-mismatch messages (empty = clean). Only function/
    method entries that HAVE a signature AND resolve to a real symbol are
    compared — absence/parse failures are grounded's job, not double-reported."""
    project_dir = Path(project_dir)
    violations: list[str] = []
    for e in codemap.entries:
        if e.kind not in ("function", "method") or not e.signature:
            continue
        fpath = project_dir / e.file
        if not fpath.is_file():
            continue
        idx = index_module(fpath)
        if idx is None:
            continue
        info = idx.get(_expected_key(e))
        if info is None:
            continue
        doc = parse_signature_params(e.signature)
        if doc is None:
            violations.append(f"{e.id}: unparseable signature {e.signature!r}")
        elif doc != info.params:
            violations.append(
                f"{e.id}: documented params {doc} != actual {info.params}"
            )
    return violations


def index_module(path: Path) -> dict[str, SymbolInfo] | None:
    """AST index of a Python file: `{qualname: SymbolInfo}`.

    qualname is the bare name for top-level defs/classes and `Class.method`
    for methods (one nesting level — sufficient for Phase 1). Returns None on
    a syntax error or unreadable file so a half-edited file mid-run can never
    crash a drift gate (cf. the no_regression fail-closed lesson).
    """
    path = Path(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    idx: dict[str, SymbolInfo] = {}
    for node in tree.body:
        if isinstance(node, _FUNC):
            idx[node.name] = SymbolInfo("function", node.lineno,
                                        _render_params(node.args))
        elif isinstance(node, ast.ClassDef):
            idx[node.name] = SymbolInfo("class", node.lineno, [])
            for sub in node.body:
                if isinstance(sub, _FUNC):
                    idx[f"{node.name}.{sub.name}"] = SymbolInfo(
                        "method", sub.lineno, _render_params(sub.args))
    return idx


def parse_codemap(path: Path) -> CodeMap:
    """Load + validate a CODEMAP.yaml. Raises CodeMapError on any problem."""
    path = Path(path)
    if not path.is_file():
        raise CodeMapError(f"CODEMAP not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise CodeMapError(f"CODEMAP invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise CodeMapError("CODEMAP: top-level must be a mapping with `entries:`")
    raw = data.get("entries")
    if not isinstance(raw, list):
        raise CodeMapError("CODEMAP: top-level `entries:` must be a list")
    entries: list[Entry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CodeMapError(f"CODEMAP entry {i} is not a mapping")
        for field in _REQUIRED:
            if field not in item:
                raise CodeMapError(f"CODEMAP entry {i} missing `{field}`")
        kind = str(item["kind"])
        if kind not in _KINDS:
            raise CodeMapError(
                f"CODEMAP entry {i}: bad kind {kind!r} (expected one of "
                f"{sorted(_KINDS)})"
            )
        try:
            line = int(item["line"])
        except (TypeError, ValueError):
            raise CodeMapError(f"CODEMAP entry {i}: line must be an int")
        sig = item.get("signature")
        entries.append(
            Entry(
                id=str(item["id"]),
                kind=kind,
                file=str(item["file"]),
                line=line,
                signature=(str(sig) if sig else None),
                summary=str(item.get("summary", "")),
            )
        )
    return CodeMap(entries=tuple(entries))
