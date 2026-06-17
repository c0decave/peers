from peers.research.ports import Witness, Claim
from peers.research.claim_ledger import (independent_origins, classify_claim,
                                         CONFIRMED, SINGLE_SOURCE, CONTESTED, UNVERIFIED_GAP)


def _w(origin, ch="h"):
    return Witness(kind="fetched-source", uri=f"https://{origin}/p", content_hash=ch, resolved_origin=origin)


def test_two_urls_same_origin_count_as_one_witness():
    # The load-bearing 5.2 rule: same resolved origin => one witness, so NOT confirmed.
    ws = [_w("a.example"), _w("a.example")]      # two URLs, ONE origin
    assert independent_origins(ws) == 1
    assert classify_claim(Claim(id="C1", text="t", status="", witnesses=ws, load_bearing=True)) == SINGLE_SOURCE


def test_two_distinct_origins_confirm():
    ws = [_w("a.example"), _w("b.example")]
    assert independent_origins(ws) == 2
    assert classify_claim(Claim(id="C1", text="t", status="", witnesses=ws, load_bearing=True)) == CONFIRMED


def test_zero_witnesses_is_unverified_gap():
    assert classify_claim(Claim(id="C1", text="t", status="", witnesses=[], load_bearing=True)) == UNVERIFIED_GAP


def test_explicit_contested_is_preserved():
    ws = [_w("a.example"), _w("b.example")]
    c = Claim(id="C1", text="t", status=CONTESTED, witnesses=ws, load_bearing=True)
    assert classify_claim(c) == CONTESTED      # contested is sticky even with 2 origins


def test_empty_origin_witness_does_not_count_toward_confirmation():
    # a witness whose fetch failed to resolve an origin (empty string)
    # must NOT count as an independent corroborator. One real origin + one
    # origin-less witness is a SINGLE source, never confirmed (tighten-only 5.2).
    ws = [_w("a.example"), _w("")]
    assert independent_origins(ws) == 1
    assert classify_claim(Claim(id="C1", text="t", status="", witnesses=ws, load_bearing=True)) == SINGLE_SOURCE


def test_only_empty_origin_witnesses_are_an_unverified_gap():
    # BUG-527 edge: two origin-less witnesses corroborate NOTHING -> 0 origins.
    ws = [_w(""), _w("", ch="h2")]
    assert independent_origins(ws) == 0
    assert classify_claim(Claim(id="C2", text="t", status="", witnesses=ws, load_bearing=True)) == UNVERIFIED_GAP
