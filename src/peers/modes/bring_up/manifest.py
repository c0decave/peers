"""The bring-up run manifest — the operator's intake for a bring-up run.

Parsed fail-closed: a top-level allow-list plus per-section validation, mirroring
:mod:`peers.spine.op_config`. The operator chooses the target tool, the corpus
adapter, the driver command, the layered oracle, landing, the calibrated memory
knobs, and the budget — never a goal (the bar emerges from the corpus + the
tool's own tests). See ``docs/plans/2026-06-12-bring-up-mode-design.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Corpus-intake adapters that yield normalised :class:`~.models.Case` streams.
CORPUS_ADAPTERS = ("exploit-corpus", "queue-file", "intake-dir", "pytest")
#: Where the driver runs the tool-under-test.
SANDBOXES = ("host", "container", "lab")
#: Pluggable, layerable oracle kinds.
ORACLE_KINDS = ("runtime", "differential", "test-suite")
#: Landing posture (branch-pr default; auto-merge opt-in).
LANDING_MODES = ("branch-pr", "auto-merge")
#: Cross-run memory mode (conservative default: hints-only).
MEMORY_MODES = ("off", "hints-only", "warm-start", "full-resume")
#: When to re-verify a memorised case verdict.
REVERIFY_MODES = ("always", "on-change", "trust-cached")
#: How widely memory is shared.
MEMORY_SCOPES = ("per-case", "per-tool", "cross-tool")


def _pos_int(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an int >= 1")
    return value


def _nonneg_int(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be an int >= 0")
    return value


def _require_mapping(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a mapping")
    return value


def _reject_unknown(d: dict, allowed: frozenset, what: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ValueError(f"unknown {what} key(s): {sorted(unknown)}")


def _nonempty_str(value, msg: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class Target:
    repo: str
    remote: str = "origin"

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        _require_mapping(d, "target")
        _reject_unknown(d, frozenset({"repo", "remote"}), "target")
        repo = _nonempty_str(d.get("repo"), "target requires a non-empty 'repo'")
        remote = d.get("remote", "origin")
        if not isinstance(remote, str) or not remote:
            raise ValueError("target remote must be a non-empty string")
        return cls(repo=repo, remote=remote)


@dataclass(frozen=True)
class Corpus:
    adapter: str
    select: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Corpus":
        _require_mapping(d, "corpus")
        _reject_unknown(d, frozenset({"adapter", "select"}), "corpus")
        adapter = _nonempty_str(d.get("adapter"),
                                "corpus requires a non-empty 'adapter'")
        if adapter not in CORPUS_ADAPTERS:
            raise ValueError(
                f"unknown corpus adapter {adapter!r}; allowed: {sorted(CORPUS_ADAPTERS)}")
        select = d.get("select", {})
        if not isinstance(select, dict):
            raise ValueError("corpus select must be a mapping")
        return cls(adapter=adapter, select=dict(select))


@dataclass(frozen=True)
class Driver:
    run_case: str
    cwd: str = "{target}"
    timeout_s: int = 1800
    sandbox: str = "container"
    image: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Driver":
        _require_mapping(d, "driver")
        _reject_unknown(
            d, frozenset({"run_case", "cwd", "timeout_s", "sandbox", "image"}),
            "driver")
        run_case = _nonempty_str(d.get("run_case"),
                                 "driver requires a non-empty 'run_case'")
        cwd = d.get("cwd", "{target}")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("driver cwd must be a non-empty string")
        timeout_s = _pos_int(d.get("timeout_s", 1800), "driver timeout_s")
        sandbox = d.get("sandbox", "container")
        if sandbox not in SANDBOXES:
            raise ValueError(
                f"unknown sandbox {sandbox!r}; allowed: {sorted(SANDBOXES)}")
        image = d.get("image")
        if image is not None and (not isinstance(image, str) or not image):
            raise ValueError("driver image must be a non-empty string or None")
        return cls(run_case=run_case, cwd=cwd, timeout_s=timeout_s,
                   sandbox=sandbox, image=image)


@dataclass(frozen=True)
class OracleSpec:
    kind: str
    config: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "OracleSpec":
        _require_mapping(d, "oracle entry")
        kind = d.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("oracle entry requires a 'kind'")
        if kind not in ORACLE_KINDS:
            raise ValueError(
                f"unknown oracle kind {kind!r}; allowed: {sorted(ORACLE_KINDS)}")
        config = {k: v for k, v in d.items() if k != "kind"}
        return cls(kind=kind, config=config)


@dataclass(frozen=True)
class Landing:
    mode: str = "branch-pr"
    branch: str = "peers/bringup/{run}"

    @classmethod
    def from_dict(cls, d: dict) -> "Landing":
        _require_mapping(d, "landing")
        _reject_unknown(d, frozenset({"mode", "branch"}), "landing")
        mode = d.get("mode", "branch-pr")
        if mode not in LANDING_MODES:
            raise ValueError(
                f"unknown landing mode {mode!r}; allowed: {sorted(LANDING_MODES)}")
        branch = d.get("branch", "peers/bringup/{run}")
        if not isinstance(branch, str) or not branch:
            raise ValueError("landing branch must be a non-empty string")
        return cls(mode=mode, branch=branch)


@dataclass(frozen=True)
class Memory:
    mode: str = "hints-only"
    reverify: str = "on-change"
    hint_budget: int = 3
    scope: str = "per-tool"

    @classmethod
    def from_dict(cls, d: dict) -> "Memory":
        _require_mapping(d, "memory")
        _reject_unknown(
            d, frozenset({"mode", "reverify", "hint_budget", "scope"}), "memory")
        mode = d.get("mode", "hints-only")
        if mode not in MEMORY_MODES:
            raise ValueError(
                f"unknown memory mode {mode!r}; allowed: {sorted(MEMORY_MODES)}")
        reverify = d.get("reverify", "on-change")
        if reverify not in REVERIFY_MODES:
            raise ValueError(
                f"unknown memory reverify {reverify!r}; allowed: {sorted(REVERIFY_MODES)}")
        hint_budget = _nonneg_int(d.get("hint_budget", 3), "memory hint_budget")
        scope = d.get("scope", "per-tool")
        if scope not in MEMORY_SCOPES:
            raise ValueError(
                f"unknown memory scope {scope!r}; allowed: {sorted(MEMORY_SCOPES)}")
        return cls(mode=mode, reverify=reverify, hint_budget=hint_budget, scope=scope)


@dataclass(frozen=True)
class BringUpBudget:
    max_rounds: int = 12
    dry_n: int = 3
    per_case_fix_budget: int = 10

    @classmethod
    def from_dict(cls, d: dict) -> "BringUpBudget":
        _require_mapping(d, "budget")
        _reject_unknown(
            d, frozenset({"max_rounds", "dry_n", "per_case_fix_budget"}), "budget")
        return cls(
            max_rounds=_pos_int(d.get("max_rounds", 12), "budget max_rounds"),
            dry_n=_pos_int(d.get("dry_n", 3), "budget dry_n"),
            per_case_fix_budget=_pos_int(
                d.get("per_case_fix_budget", 10), "budget per_case_fix_budget"),
        )


_REQUIRED = ("target", "corpus", "driver", "oracle")
_ALLOWED = frozenset(
    {"target", "corpus", "driver", "oracle", "landing", "memory", "budget"})


@dataclass(frozen=True)
class BringUpManifest:
    """A validated bring-up run intake."""

    target: Target
    corpus: Corpus
    driver: Driver
    oracle: tuple[OracleSpec, ...]
    landing: Landing = field(default_factory=Landing)
    memory: Memory = field(default_factory=Memory)
    budget: BringUpBudget = field(default_factory=BringUpBudget)


def load_manifest(d: dict) -> BringUpManifest:
    """Validate + normalise a raw manifest mapping into a :class:`BringUpManifest`."""
    _require_mapping(d, "manifest")
    _reject_unknown(d, _ALLOWED, "manifest")
    for section in _REQUIRED:
        if section not in d:
            raise ValueError(f"manifest requires '{section}'")

    oracle_raw = d["oracle"]
    if not isinstance(oracle_raw, list) or not oracle_raw:
        raise ValueError("oracle must be a non-empty list")
    oracle = tuple(OracleSpec.from_dict(o) for o in oracle_raw)

    return BringUpManifest(
        target=Target.from_dict(d["target"]),
        corpus=Corpus.from_dict(d["corpus"]),
        driver=Driver.from_dict(d["driver"]),
        oracle=oracle,
        landing=Landing.from_dict(d.get("landing", {})),
        memory=Memory.from_dict(d.get("memory", {})),
        budget=BringUpBudget.from_dict(d.get("budget", {})),
    )
