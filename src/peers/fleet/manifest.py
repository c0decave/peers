"""Fleet daemon manifest — the operator's intake for a `peers-ctl fleet` run.

Parsed fail-closed (mirrors :mod:`peers.modes.bring_up.manifest` +
:mod:`peers.spine.op_config`): a top-level allow-list, per-section validation,
and a final :func:`peers.fleet.program.validate_program` pass so a malformed
program NEVER yields a partial schedule. Produces the exact typed inputs the
daemon threads into :func:`peers.fleet.conductor.conduct_tick`:

  * ``program``     — a validated :class:`~peers.fleet.program.Program`
  * ``pool``        — the :class:`~peers.fleet.scheduler.Pool` (slots + affinity)
  * ``ceiling``     — the :class:`~peers.fleet.scheduler.Ceiling` (aggregate cap)
  * ``repos_by_id`` — ``{run_id: Path(tool)}`` for the conductor's substrate ops
  * ``daemon``      — the loop knobs (:class:`FleetDaemonConfig`)

See ``docs/plans/2026-06-13-fleet-daemon-design.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from peers.fleet.program import ModeRunSpec, Program, validate_program
from peers.fleet.scheduler import Ceiling, Pool
from peers.spine.op_config import OpConfig


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


def _pos_int_or_none(value, name: str):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an int >= 1 or null")
    return value


def _pos_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a number > 0")
    return float(value)


@dataclass(frozen=True)
class FleetDaemonConfig:
    """The loop knobs. ``max_ticks=None`` runs until terminal; the rolling idle
    timeout (NOT a wall-clock kill) decides a wedged run."""

    max_ticks: int | None = None
    tick_sleep_s: float = 5.0
    idle_timeout_s: int = 900
    target_ref: str = "main"

    @classmethod
    def from_dict(cls, d: dict) -> "FleetDaemonConfig":
        _require_mapping(d, "daemon")
        _reject_unknown(
            d, frozenset({"max_ticks", "tick_sleep_s", "idle_timeout_s",
                          "target_ref"}), "daemon")
        target_ref = d.get("target_ref", "main")
        if not isinstance(target_ref, str) or not target_ref:
            raise ValueError("daemon target_ref must be a non-empty string")
        return cls(
            max_ticks=_pos_int_or_none(d.get("max_ticks"), "daemon max_ticks"),
            tick_sleep_s=_pos_number(d.get("tick_sleep_s", 5.0),
                                     "daemon tick_sleep_s"),
            idle_timeout_s=_pos_int_or_none(d.get("idle_timeout_s", 900),
                                            "daemon idle_timeout_s"),
            target_ref=target_ref,
        )


@dataclass(frozen=True)
class FleetManifest:
    """A validated fleet run intake."""

    program: Program
    pool: Pool
    ceiling: Ceiling
    repos_by_id: dict[str, Path]
    daemon: FleetDaemonConfig


_ALLOWED_TOP = frozenset({"pool", "ceiling", "daemon", "runs"})
_ALLOWED_RUN = frozenset(
    {"run_id", "tool", "mode", "depends_on", "landing", "affinity", "writable",
     "requires_artifact", "budget"})


def _spec_from_dict(d: dict) -> ModeRunSpec:
    _require_mapping(d, "run")
    _reject_unknown(d, _ALLOWED_RUN, "run")
    run_id = _nonempty_str(d.get("run_id"), "run requires a non-empty 'run_id'")
    tool = _nonempty_str(d.get("tool"), f"run {run_id!r} requires a non-empty 'tool'")
    mode = _nonempty_str(d.get("mode"), f"run {run_id!r} requires a non-empty 'mode'")

    depends_on = d.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(x, str) for x in depends_on):
        raise ValueError(f"run {run_id!r} depends_on must be a list of run-id strings")

    writable = d.get("writable", True)
    if not isinstance(writable, bool):
        raise ValueError(f"run {run_id!r} writable must be a bool")

    affinity = d.get("affinity")
    if affinity is not None and (not isinstance(affinity, str) or not affinity):
        raise ValueError(f"run {run_id!r} affinity must be a non-empty string or null")

    requires_artifact = d.get("requires_artifact")
    if requires_artifact is not None and (
            not isinstance(requires_artifact, str) or not requires_artifact):
        raise ValueError(
            f"run {run_id!r} requires_artifact must be a non-empty string or null")

    oc: dict = {"mode": mode}
    if "landing" in d:
        oc["landing"] = d["landing"]
    if "budget" in d:
        oc["budget"] = _require_mapping(d["budget"], f"run {run_id!r} budget")
    # OpConfig.from_dict validates the mode allow-list + landing fail-closed.
    op_config = OpConfig.from_dict(oc)

    return ModeRunSpec(
        tool=Path(tool), mode=mode, op_config=op_config, run_id=run_id,
        depends_on=list(depends_on), affinity=affinity, writable=writable,
        requires_artifact=requires_artifact)


def _pool_from_dict(d: dict) -> Pool:
    _require_mapping(d, "pool")
    _reject_unknown(d, frozenset({"slots", "affinity"}), "pool")
    slots = d.get("slots")
    if (not isinstance(slots, list) or not slots
            or not all(isinstance(s, str) and s for s in slots)):
        raise ValueError("pool requires a non-empty 'slots' list of slot-id strings")
    if len(set(slots)) != len(slots):
        raise ValueError(f"pool slots must be unique: {slots}")
    affinity = d.get("affinity", {})
    if not isinstance(affinity, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in affinity.items()):
        raise ValueError("pool affinity must be a {label: slot-id} string mapping")
    for label, slot in affinity.items():
        if slot not in slots:
            raise ValueError(
                f"pool affinity {label!r} -> {slot!r} is not a declared slot")
    return Pool(slots=list(slots), affinity=dict(affinity))


def _ceiling_from_dict(d: dict) -> Ceiling:
    _require_mapping(d, "ceiling")
    _reject_unknown(d, frozenset({"max_tokens", "max_runs"}), "ceiling")
    return Ceiling(
        max_tokens=_pos_int_or_none(d.get("max_tokens"), "ceiling max_tokens"),
        max_runs=_pos_int_or_none(d.get("max_runs"), "ceiling max_runs"),
    )


def load_fleet_manifest(raw: dict) -> FleetManifest:
    """Validate + normalise a raw fleet manifest mapping into a
    :class:`FleetManifest`. Raises :class:`ValueError` on any defect (fail-closed:
    a bad program never yields a partial schedule)."""
    _require_mapping(raw, "manifest")
    _reject_unknown(raw, _ALLOWED_TOP, "manifest")

    runs_raw = raw.get("runs")
    if not isinstance(runs_raw, list) or not runs_raw:
        raise ValueError("manifest requires a non-empty 'runs' list")
    specs = [_spec_from_dict(r) for r in runs_raw]
    program = Program(runs=specs)

    ok, errors = validate_program(program)
    if not ok:
        raise ValueError("invalid fleet program: " + "; ".join(errors))

    if "pool" not in raw:
        raise ValueError("manifest requires a 'pool'")
    pool = _pool_from_dict(raw["pool"])
    # Fail-closed: a run's affinity label MUST be declared in the pool — else the
    # scheduler silently never assigns it a slot and the run starves unobserved.
    for s in specs:
        if s.affinity is not None and s.affinity not in pool.affinity:
            raise ValueError(
                f"run {s.run_id!r} requests unknown affinity label {s.affinity!r}; "
                f"pool declares {sorted(pool.affinity)}")
    ceiling = _ceiling_from_dict(raw.get("ceiling", {}))
    daemon = FleetDaemonConfig.from_dict(raw.get("daemon", {}))
    repos_by_id = {s.run_id: Path(s.tool) for s in specs}

    return FleetManifest(program=program, pool=pool, ceiling=ceiling,
                         repos_by_id=repos_by_id, daemon=daemon)
