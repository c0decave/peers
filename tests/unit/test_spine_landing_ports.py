from peers.spine.landing import (LandingContract, MERGEABLE, NOT_MERGEABLE)

def test_landing_contract_shape_and_dict_default_is_independent():
    a = LandingContract(branch="feat/x", mergeable=True, gates={"ModeRun-valid": True},
                        landing_mode="branch-pr", mode_run="r1", head_sha="ab" * 20,
                        self_hosting=False)
    assert a.branch == "feat/x" and a.mergeable is True
    assert a.gates == {"ModeRun-valid": True} and a.landing_mode == "branch-pr"
    # field(default_factory=dict) — NOT a shared mutable default:
    b = LandingContract(branch="feat/y", mergeable=False, landing_mode="branch-pr",
                        mode_run="r1")
    b.gates["x"] = True
    c = LandingContract(branch="feat/z", mergeable=False, landing_mode="branch-pr",
                        mode_run="r1")
    assert c.gates == {}                       # b's mutation did not leak into c
    assert c.head_sha is None and c.self_hosting is False        # scalar defaults

def test_mergeable_sentinels():
    assert MERGEABLE == "mergeable" and NOT_MERGEABLE == "not-mergeable"
