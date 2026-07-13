import pytest
from optimizer.optimizer import Optimizer
from optimizer.opportunity import Opportunity, OpportunityTier


def make_tier(quantity=100, leg_prices=None, capital=1000, profit=100):
    return OpportunityTier(
        quantity=quantity,
        leg_prices=leg_prices or {"A": 500, "B": 500},
        capital_required=capital,
        profit=profit,
    )


def make_opp(event_id, profit=100, capital=1000, quantity=100):
    tier = make_tier(quantity=quantity, capital=capital, profit=profit)
    return Opportunity(event_id=event_id, side="buy", tiers=[tier])


def test_allocate_highest_roi_first():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=100, capital=1000))
    opt.update("E2", make_opp("E2", profit=200, capital=1000))

    result = opt.allocate(3000)
    event_ids = [ev for ev, _ in result]
    assert event_ids.index("E2") < event_ids.index("E1")


def test_allocate_deploys_capital():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=100, capital=1000))
    opt.update("E2", make_opp("E2", profit=200, capital=1000))

    result = opt.allocate(2000)
    assert len(result) == 2


def test_partial_fill_on_last_tier():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=100, capital=1000, quantity=100))
    opt.update("E2", make_opp("E2", profit=200, capital=1000, quantity=100))

    result = opt.allocate(1500)
    assert len(result) == 2
    partial_ev, partial_tier = [(ev, t) for ev, t in result if ev == "E1"][0]
    assert partial_tier.quantity == 50
    assert partial_tier.capital_required == 500


def test_respects_committed_capital():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=100, capital=1000))
    opt.update("E2", make_opp("E2", profit=200, capital=1000))
    opt.commit("X", 500)  # 500 committed elsewhere

    result = opt.allocate(2000)
    assert len(result) == 2
    partial_ev, partial_tier = [(ev, t) for ev, t in result if ev == "E1"][0]
    assert partial_tier.quantity == 50


def test_returns_empty_when_no_capital():
    opt = Optimizer()
    opt.commit("Y", 1000)
    opt.update("E1", make_opp("E1"))
    assert opt.allocate(1000) == []


def test_update_none_removes_opportunity():
    opt = Optimizer()
    opt.update("E1", make_opp("E1"))
    opt.update("E1", None)
    assert opt.allocate(5000) == []


def test_release_frees_committed():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=100, capital=1000))
    opt.commit("E1", 1000)
    assert opt.allocate(1000) == []
    opt.release("E1")
    result = opt.allocate(1000)
    assert len(result) == 1


def test_allocate_multi_tier_selects_only_one_per_event():
    tier1 = make_tier(quantity=500, capital=48000, profit=1500)
    tier2 = make_tier(quantity=91,  capital=8900,  profit=250)
    opp = Opportunity(event_id="E1", side="buy", tiers=[tier1, tier2])
    opt = Optimizer()
    opt.update("E1", opp)
    result = opt.allocate(100_000)
    event_ids = [ev for ev, _ in result]
    assert event_ids.count("E1") == 1


def test_allocate_skips_committed_event():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=200, capital=1000))
    opt.commit("E1", 1000)
    result = opt.allocate(10_000)
    assert all(ev != "E1" for ev, _ in result)


def test_allocate_multi_tier_committed_after_first_prevents_second():
    tier1 = make_tier(quantity=500, capital=48000, profit=1500)
    tier2 = make_tier(quantity=91,  capital=8900,  profit=250)
    opp = Opportunity(event_id="E1", side="buy", tiers=[tier1, tier2])
    opt = Optimizer()
    opt.update("E1", opp)
    result = opt.allocate(100_000)
    for ev, tier in result:
        opt.commit(ev, tier.capital_required)
    result2 = opt.allocate(100_000)
    assert all(ev != "E1" for ev, _ in result2)


def test_allocate_excludes_open_position_events():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=200, capital=1000))
    opt.update("E2", make_opp("E2", profit=100, capital=1000))
    result = opt.allocate(10_000, exclude_events={"E1"})
    event_ids = [ev for ev, _ in result]
    assert "E1" not in event_ids
    assert "E2" in event_ids


def test_allocate_exclude_events_none_means_no_exclusions():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=200, capital=1000))
    result = opt.allocate(10_000, exclude_events=None)
    assert len(result) == 1


def test_event_cap_shrinks_oversized_tier():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=500, capital=5000, quantity=500))

    result = opt.allocate(10_000, max_event_capital=1000)

    assert len(result) == 1
    _, tier = result[0]
    assert tier.capital_required <= 1000
    assert tier.quantity == 100  # 1000 budget // 10 unit capital


def test_event_cap_still_allows_multiple_events():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=500, capital=5000, quantity=500))
    opt.update("E2", make_opp("E2", profit=400, capital=5000, quantity=500))

    result = opt.allocate(10_000, max_event_capital=1000)

    # each event capped individually; the cap must not end allocation early
    assert len(result) == 2
    assert all(t.capital_required <= 1000 for _, t in result)


def test_no_event_cap_behaves_as_before():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=500, capital=5000, quantity=500))
    result = opt.allocate(10_000)
    assert result[0][1].capital_required == 5000


def test_event_cap_respects_available_when_smaller():
    opt = Optimizer()
    opt.update("E1", make_opp("E1", profit=500, capital=5000, quantity=500))
    result = opt.allocate(300, max_event_capital=1000)
    _, tier = result[0]
    assert tier.capital_required <= 300
