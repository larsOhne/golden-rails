import golden_rails.evaluate as ev
from golden_rails.network import load_network
from golden_rails.sim import DisruptionModel, build_trains, plan_timetable, simulate_day


def _quiet():
    dm = DisruptionModel()
    dm.p_origin = {"FV": 0, "NV": 0, "GV": 0}
    dm.p_dwell = 0
    dm.run_sd = 0
    dm.rate_full_closure = 0
    dm.rate_track_fault = 0
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
    assert r["delay_min"] < 1e-6


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
