"""Task 6: `--no-codemap` opt-out flag on both CLIs.

The `run` (peers) and `start` (peers-ctl) subcommands accept
`--no-codemap` as a store_true defaulting to False. The peers side
plumbs it to the driver as `codemap_enabled=not no_codemap`; the
peers-ctl forwarder appends `--no-codemap` to the subprocess extras.

Note: `peers run` takes its target via the top-level `-C/--target`
flag, not a positional — so the parser is exercised as
`["run", ...]`, mirroring the existing `--without-recon` tests.
"""

from peers.cli import build_parser
from peers_ctl.cli import build_parser as build_ctl_parser


def test_run_parser_has_no_codemap_flag():
    parser = build_parser()
    args = parser.parse_args(["run", "--no-codemap"])
    assert args.no_codemap is True
    args2 = parser.parse_args(["run"])
    assert args2.no_codemap is False


def test_ctl_start_parser_has_no_codemap_flag():
    parser = build_ctl_parser()
    args = parser.parse_args(["start", "proj", "--no-codemap"])
    assert args.no_codemap is True
    args2 = parser.parse_args(["start", "proj"])
    assert args2.no_codemap is False


def test_ctl_start_forwards_no_codemap_to_extras(monkeypatch):
    """cmd_start(no_codemap=True) appends `--no-codemap` to the
    subprocess extra_args (without launching a real subprocess)."""
    import peers_ctl.cli as ctl

    captured: dict = {}

    class _FakeProject:
        path = "/nonexistent"
        log_path = "/nonexistent/log"

    class _FakeStore:
        def get(self, name):
            return _FakeProject()

    def fake_start_project(store, p, **kwargs):
        captured["extra_args"] = kwargs.get("extra_args")
        return 4321

    monkeypatch.setattr(ctl, "_store", lambda config_dir=None: _FakeStore())
    monkeypatch.setattr(ctl, "reconcile", lambda store: None)
    monkeypatch.setattr(ctl, "start_project", fake_start_project)

    rc = ctl.cmd_start("proj", no_codemap=True)

    assert rc == 0
    assert "--no-codemap" in captured["extra_args"]

    # Default: flag absent.
    rc2 = ctl.cmd_start("proj")
    assert rc2 == 0
    assert "--no-codemap" not in captured["extra_args"]
