"""Tests for niche discovery via Gamma events.

These lock in the behaviour learned from the live API: categorisation comes from
an event's *tags* (not the market's empty top-level tags), events are scanned
with offset pagination and ranked by volume locally, and holders of the matched
markets become the candidate pool.
"""

from __future__ import annotations

from conftest import FakeClient, make_event
from pmwatch.discover import discover_candidates, event_matches_niche


def test_event_matches_niche_on_tags(cfg) -> None:
    politics = cfg.niche("politics")
    crypto = cfg.niche("crypto")
    # An event tagged "US Politics" should match the politics niche...
    ev = make_event("e", tags=["US Politics", "Trump"], condition_ids=["m"])
    assert event_matches_niche(ev, politics) is True
    # ...but not the crypto niche.
    assert event_matches_niche(ev, crypto) is False


def test_event_matches_niche_on_title_fallback(cfg) -> None:
    # No useful tags, but the title carries the keyword.
    ev = make_event("e", tags=[], condition_ids=["m"], title="2024 Crypto regulation bill")
    assert event_matches_niche(ev, cfg.niche("crypto")) is True


def test_discover_collects_holders_of_matching_events(cfg) -> None:
    politics = cfg.niche("politics")
    events = [
        make_event("e1", tags=["Politics"], condition_ids=["mA"], volume=500.0),
        make_event("e2", tags=["Crypto"], condition_ids=["mB"], volume=900.0),  # wrong niche
        make_event("e3", tags=["Elections"], condition_ids=["mC"], volume=999.0),
    ]
    holders = {
        "mA": ["0x1", "0x2"],
        "mB": ["0x9"],  # must NOT appear (crypto event)
        "mC": ["0x2", "0x3"],  # 0x2 overlaps mA -> deduped
    }
    client = FakeClient(events=events, holders=holders)

    candidates = discover_candidates(client, politics, cfg.discovery)

    assert "0x9" not in candidates  # crypto event excluded
    assert set(candidates) == {"0x1", "0x2", "0x3"}  # union of mA + mC, deduped
    assert len(candidates) == 3  # no duplicate 0x2


def test_discover_paginates_until_empty(cfg) -> None:
    # 250 politics events spread across 3 pages; discovery should see them all.
    events = [make_event(f"e{i}", tags=["Politics"], condition_ids=[f"m{i}"]) for i in range(250)]
    holders = {f"m{i}": [f"0xw{i}"] for i in range(250)}
    client = FakeClient(events=events, holders=holders)

    # markets_per_niche caps how many markets we actually pull holders from.
    candidates = discover_candidates(client, cfg.niche("politics"), cfg.discovery, event_pages=3)
    assert len(candidates) == cfg.discovery.markets_per_niche
