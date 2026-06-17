import hashlib
from peers.spine.baseline_adapter import CharacterizationAuthor
from peers.spine.baseline import CandidateBaseline, BaselineAuthor
from peers.spine.direction import Bar

def test_adapter_is_a_baseline_author():
    assert isinstance(CharacterizationAuthor(render=lambda repo, bar: None), BaselineAuthor)

def test_adapter_writes_a_candidate_and_rehashes_stably(tmp_path):
    body = "def test_obs():\n    assert (1, 2) == (1, 2)\n"
    author = CharacterizationAuthor(render=lambda repo, bar: body)
    cand = author.author(tmp_path, Bar("absent", None))
    assert isinstance(cand, CandidateBaseline)
    assert cand.command == "python3 -m pytest test_characterization.py"
    on_disk = (tmp_path / "test_characterization.py").read_bytes()
    assert on_disk.decode() == body
    assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(body.encode()).hexdigest()

def test_adapter_none_render_returns_none(tmp_path):
    # The generator cannot characterize this tool -> author returns None -> the
    # builder will map this to uncharacterizable (honest stop). No file written.
    author = CharacterizationAuthor(render=lambda repo, bar: None)
    assert author.author(tmp_path, Bar("absent", None)) is None
    assert not (tmp_path / "test_characterization.py").exists()
