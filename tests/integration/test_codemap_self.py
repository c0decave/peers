from pathlib import Path

from peers.codemap import (
    check_complete,
    check_grounded,
    check_signatures,
    parse_codemap,
)
from peers.codemap_gen import build_structural_codemap, serialize_codemap

# Repo root = three levels up from this test file (tests/integration/..).
REPO = Path(__file__).resolve().parents[2]


def test_peers_own_codemap_is_clean(tmp_path):
    cm = build_structural_codemap(REPO)
    assert len(cm.entries) > 50  # peers has a substantial public surface
    yaml_path = tmp_path / "CODEMAP.yaml"
    yaml_path.write_text(serialize_codemap(cm), encoding="utf-8")
    reparsed = parse_codemap(yaml_path)
    assert check_grounded(REPO, reparsed) == []
    assert check_signatures(REPO, reparsed) == []
    assert check_complete(REPO, reparsed) == []
