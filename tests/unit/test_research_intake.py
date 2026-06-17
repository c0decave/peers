"""STEP-2 — generic TOPIC.md intake gate (a relaxed, no-security-frame
``topic_present`` check). require_topic is fail-CLOSED and
symlink-refusing; it requires non-vacuous ``## Scope`` + ``## Questions`` but NOT
``## Frameworks`` (so a non-security topic passes)."""
from peers.research.intake import require_topic


def test_generic_topic_without_frameworks_passes(tmp_path):
    (tmp_path / "TOPIC.md").write_text(
        "# T\n\n## Scope\n" + "x" * 80 + "\n\n## Questions\n" + "y" * 80 + "\n")
    ok, problems = require_topic(tmp_path)
    assert ok is True
    assert problems == []


def test_missing_topic_fails_closed(tmp_path):
    ok, problems = require_topic(tmp_path)
    assert ok is False
    assert any("TOPIC.md" in p for p in problems)


def test_symlinked_topic_refused(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("## Scope\n" + "x" * 80 + "\n## Questions\n" + "y" * 80)
    link = tmp_path / "TOPIC.md"
    link.symlink_to(real)
    ok, problems = require_topic(tmp_path)
    assert ok is False


def test_vacuous_section_fails(tmp_path):
    (tmp_path / "TOPIC.md").write_text("## Scope\nshort\n## Questions\nq\n")
    ok, problems = require_topic(tmp_path)
    assert ok is False


# ---- extra 3-class coverage beyond the contract's four canonical cases ----
def test_topic_missing_questions_section_fails(tmp_path):
    # one required section present, the other absent -> fail-closed naming it.
    (tmp_path / "TOPIC.md").write_text("## Scope\n" + "x" * 80 + "\n")
    ok, problems = require_topic(tmp_path)
    assert ok is False
    assert any("Questions" in p for p in problems)


def test_extra_frameworks_section_still_accepted(tmp_path):
    # a security-style brief carrying ## Frameworks must NOT be rejected — the
    # research frame is relaxed; the extra section is simply ignored.
    (tmp_path / "TOPIC.md").write_text(
        "## Scope\n" + "x" * 80
        + "\n## Questions\n" + "y" * 80
        + "\n## Frameworks\n" + "z" * 80 + "\n")
    ok, problems = require_topic(tmp_path)
    assert ok is True
    assert problems == []


def test_unicode_topic_body_accepted(tmp_path):
    # a non-ASCII brief decodes and clears the per-section char floor (edge:
    # multi-byte content must count by decoded chars, not raw bytes).
    (tmp_path / "TOPIC.md").write_text(
        "## Scope\n" + "die Klärung über Spargel — ❄ " * 5
        + "\n## Questions\n" + "Wächst Spargel aus Stecklingen? — ¿sí? " * 4 + "\n",
        encoding="utf-8")
    ok, problems = require_topic(tmp_path)
    assert ok is True
    assert problems == []
