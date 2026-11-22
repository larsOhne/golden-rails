import golden_rails.evaluate as ev
from golden_rails.network import load_network
from golden_rails.sim import DisruptionModel, build_trains, plan_timetable, simulate_day


def _quiet():
    dm = DisruptionModel()
    dm.p_origin = {"FV": 0, "NV": 0, "S": 0, "GV": 0}
    dm.p_dwell = 0
    dm.run_sd = 0
    dm.rate_full_closure = 0
    dm.rate_track_fault = 0
    dm.seg_extra = {}
    dm.p_fail_pre = {"FV": 0, "NV": 0, "S": 0}
    dm.fail_per_km = {"FV": 0, "NV": 0, "S": 0}
    return dm


def test_data_consistent_and_routable():
    net = load_network()
    trains = build_trains(net)
    assert len(trains) > 5000
    assert all(tr.steps for tr in trains)


def test_no_disruption_means_no_delay():
    net = load_network()
    trains = build_trains(net)
    plan_timetable(net, trains)
    r = simulate_day(net, trains, _quiet(), seed=1)
    # node bookings are made in a different order than in planning: tiny residual conflicts remain
    assert r["delay_min"] / len(trains) < 0.1


def test_fixed_timetable_kept_for_track_upgrades():
    base = load_network()
    bt = build_trains(base)
    plan_timetable(base, bt)
    net = load_network()
    net.apply_changes("tracks:OG-FR:3;tracks:ROT-VER:2")
    trains = build_trains(net)
    info = plan_timetable(net, trains, {t.id: t for t in bt})
    assert info["n_replanned"] == 0


def test_new_line_attracts_long_distance_trains():
    base = load_network()
    bt = build_trains(base)
    plan_timetable(base, bt)
    net = load_network()
    net.apply_changes("new:H:BI:95:2:300:FV")
    trains = build_trains(net)
    info = plan_timetable(net, trains, {t.id: t for t in bt})
    assert info["n_replanned"] > 0
    assert any(st[0].endswith("NEW") for tr in trains for st in tr.steps)


def test_common_random_numbers_reproducible():
    net = load_network()
    trains = build_trains(net)
    plan_timetable(net, trains)
    dm = DisruptionModel()
    assert simulate_day(net, trains, dm, 7)["delay_min"] == simulate_day(net, trains, dm, 7)["delay_min"]


def test_annuity():
    assert 0.026 < ev.annuity_factor() < 0.028


def test_corridor_width_layouts():
    from golden_rails.network import channel_layout
    assert channel_layout(1) == [0]
    assert channel_layout(3) == [1, -1, 0]
    assert channel_layout(6) == [1, 1, 1, -1, -1, -1]


def test_cost_model_terrain_and_landuse():
    from golden_rails.costs import etcs_upgrade_cost, track_addition_cost
    net = load_network()
    flat_rural = track_addition_cost(net.segments["LG-UE"], 1)
    urban = track_addition_cost(net.segments["DO-BO"], 1)
    gorge = track_addition_cost(net.segments["KO-MZ"], 1)
    per_km = lambda c, s: c["total"] / net.segments[s].length
    assert per_km(flat_rural, "LG-UE") < per_km(urban, "DO-BO") < per_km(gorge, "KO-MZ")
    # ETCS: constant per track-km regardless of topography / land use
    s1, s2 = net.segments["LG-UE"], net.segments["KO-MZ"]
    e1, e2 = etcs_upgrade_cost(s1), etcs_upgrade_cost(s2)
    assert abs(e1["total"] / (s1.length * s1.tracks) - e2["total"] / (s2.length * s2.tracks)) < 1e-9


def test_priority_rule_favours_ice():
    net = load_network()
    trains = build_trains(net)
    plan_timetable(net, trains)
    dm = DisruptionModel()
    fcfs = [simulate_day(net, trains, dm, s, priority=False) for s in range(3)]
    prio = [simulate_day(net, trains, dm, s, priority=True) for s in range(3)]
    assert sum(r["punct_FV"] for r in prio) > sum(r["punct_FV"] for r in fcfs)
    assert sum(r["punct_GV"] for r in prio) < sum(r["punct_GV"] for r in fcfs)


def test_etcs_reduces_delay():
    base = load_network()
    bt = build_trains(base)
    plan_timetable(base, bt)
    net = load_network()
    net.apply_changes("etcs:N-WÜ")
    trains = build_trains(net)
    plan_timetable(net, trains, {t.id: t for t in bt})
    dm = DisruptionModel()
    a = sum(simulate_day(base, bt, dm, s)["pax_delay_min"] for s in range(4))
    b = sum(simulate_day(net, trains, dm, s)["pax_delay_min"] for s in range(4))
    assert b < a


def test_trace_does_not_change_results():
    net = load_network()
    trains = build_trains(net)
    plan_timetable(net, trains)
    dm = DisruptionModel()
    trace = []
    a = simulate_day(net, trains, dm, 11)
    b = simulate_day(net, trains, dm, 11, trace=trace)
    assert a["pax_delay_min"] == b["pax_delay_min"]
    assert len(trace) > 10000 and "incidents" in b


def test_etcs_only_lines_carry_only_equipped_trains():
    net = load_network()
    net.apply_changes("etcs2:N-WÜ;fit:Fernverkehr ICE;fit:Fernverkehr IC/EC;fit:Güterverkehr")
    trains = build_trains(net)
    for tr in trains:
        if any(net.segments[st[0]].etcs_only for st in tr.steps):
            assert tr.equipped or tr.ttype in ("NV", "S"), tr.id


def test_mixed_traffic_headway():
    net = load_network()
    seg = net.segments["N-WÜ"]
    seg.etcs = True
    assert seg.hw(True) < seg.hw(False)


def test_reserve_reduces_cancellations_and_delay():
    from golden_rails.sim import DisruptionModel
    base = load_network()
    bt = build_trains(base)
    plan_timetable(base, bt)
    dm = DisruptionModel()
    dm.p_fail_pre = {"FV": 0.2, "NV": 0.0, "S": 0.0}
    net = load_network()
    net.apply_changes("reserve:F:Fernverkehr ICE:3;reserve:K:Fernverkehr ICE:3;reserve:M:Fernverkehr ICE:3")
    trains = build_trains(net)
    plan_timetable(net, trains, {t.id: t for t in bt})
    a = [simulate_day(base, bt, dm, s) for s in range(2)]
    b = [simulate_day(net, trains, dm, s) for s in range(2)]
    assert sum(r["n_reserve_used"]["fail"] for r in b) > 0
    assert sum(r["cancel_share_FV"] for r in b) < sum(r["cancel_share_FV"] for r in a)


def test_circulations_link_vehicles():
    net = load_network()
    trains = build_trains(net)
    linked = [t for t in trains if t.pred is not None]
    assert len(linked) > 0.5 * len(trains)
    for t in linked[:500]:
        p = trains[t.pred]
        assert p.stops[-1] == t.stops[0] or t.pred_kind == "cont"
        assert t.desired_dep >= p.sched_arr
