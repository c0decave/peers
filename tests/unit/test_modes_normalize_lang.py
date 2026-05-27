"""Tests for peers.modes.normalize_lang() — lang alias normalization.

This was originally inline in cli.py:cmd_init; hoisted out into
peers.modes so other consumers (peers-ctl modes list, future --lang
args) don't have to duplicate the mapping.
"""
from __future__ import annotations


def test_normalize_lang_canonical_passthrough():
    from peers.modes import normalize_lang
    assert normalize_lang("python") == "python"
    assert normalize_lang("js") == "js"
    assert normalize_lang("rust") == "rust"
    assert normalize_lang("go") == "go"


def test_normalize_lang_aliases():
    from peers.modes import normalize_lang
    assert normalize_lang("javascript") == "js"
    assert normalize_lang("typescript") == "js"
    assert normalize_lang("ts") == "js"
    assert normalize_lang("golang") == "go"
    assert normalize_lang("rs") == "rust"
    assert normalize_lang("py") == "python"


def test_normalize_lang_case_insensitive():
    from peers.modes import normalize_lang
    assert normalize_lang("Python") == "python"
    assert normalize_lang("JS") == "js"
    assert normalize_lang("JavaScript") == "js"


def test_normalize_lang_unknown_stays_unchanged():
    # Downcased but not mapped — upstream code can warn the user.
    from peers.modes import normalize_lang
    assert normalize_lang("cobol") == "cobol"


def test_normalize_lang_empty_defaults_to_python():
    from peers.modes import normalize_lang
    assert normalize_lang("") == "python"
    assert normalize_lang(None) == "python"
