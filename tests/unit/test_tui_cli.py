"""Wave-1a: tui subcommand wiring + lazy-import guard."""
import importlib


def test_cmd_tui_missing_textual_prints_hint(monkeypatch, capsys):
    from peers_ctl import cli
    real_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "peers_ctl.tui.app" or name.startswith("textual"):
            raise ImportError("no textual")
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    rc = cli.cmd_tui(config_dir=None)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert rc == 1
    assert "pip install" in out and "[tui]" in out


def test_cmd_tui_delegates_to_app_run(monkeypatch):
    # Characterization test for the success branch: when peers_ctl.tui.app
    # imports cleanly, cmd_tui must delegate to its run(config_dir=...) and
    # return that rc verbatim. Stubs the module so no real Textual is needed.
    import types

    from peers_ctl import cli

    calls = {}
    stub = types.ModuleType("peers_ctl.tui.app")

    def _run(config_dir=None):
        calls["config_dir"] = config_dir
        return 0

    stub.run = _run  # type: ignore[attr-defined]

    real_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "peers_ctl.tui.app":
            return stub
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    rc = cli.cmd_tui(config_dir=None)
    assert rc == 0
    assert "config_dir" in calls
    assert calls["config_dir"] is None


def test_tui_subcommand_registered():
    # The peers-ctl parser stores the subcommand name under dest="cmd"
    # (see build_parser -> add_subparsers(dest="cmd")), not "command".
    from peers_ctl import cli
    parser = cli.build_parser()
    ns = parser.parse_args(["tui"])
    assert ns.cmd == "tui"
