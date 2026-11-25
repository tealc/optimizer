import copy

import pytest

from optimizer.app import app
from optimizer.optimizer import BatteryConfig, GridConfig, OptimizationStrategy, Optimizer, TimeSeriesData

T = 24
AWAY = range(8, 17)
P_A = 0.00035


def request(p_N, s_initial=20000., p_a=P_A, **battery):
    bat = {'c_max': 11000, 'c_min': 4140, 'charge_from_grid': True, 'd_max': 0, 'p_a': p_a,
           's_initial': s_initial, 's_max': 60000, 's_min': 0, 's_capacity': 60000, **battery}
    return {'batteries': [bat], 'eta_c': 0.9, 'eta_d': 0.9, 'grid': {},
            'strategy': {'charging_strategy': 'none', 'discharging_strategy': 'none'},
            'time_series': {'dt': [3600] * T, 'gt': [500] * T, 'ft': [0] * T, 'p_N': p_N, 'p_E': [0.00005] * T}}


def solve(req):
    response = app.test_client().post('/optimize/charge-schedule', json=req)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def availability():
    return [0 if t in AWAY else 1 for t in range(T)]


def driving(total=18000.):
    return [total / len(AWAY) if t in AWAY else 0. for t in range(T)]


# cheap while the vehicle is away and dear after it returns, so the only other choice is before it leaves
CHEAP_WHILE_AWAY = [0.0001 if t in AWAY else 0.0005 if t >= 17 else 0.0003 - t * 1e-6 for t in range(T)]
# cheap after the vehicle is back
CHEAP_AFTER_RETURN = [0.0001 + t * 1e-6 if t >= 17 else 0.0003 for t in range(T)]


@pytest.fixture(autouse=True)
def closed_gap(monkeypatch):
    monkeypatch.setenv('OPTIMIZER_GAP_ABS', '0')


def test_charges_before_leaving_when_it_is_cheapest_while_away():
    free = solve(request(CHEAP_WHILE_AWAY))['batteries'][0]['charging_power']
    assert sum(free[t] for t in AWAY) > 0, 'without availability the cheap window must attract charging'

    result = solve(request(CHEAP_WHILE_AWAY, c_avail=availability()))
    charging = result['batteries'][0]['charging_power']
    assert all(charging[t] == pytest.approx(0, abs=1e-6) for t in AWAY)
    assert sum(charging[:8]) > 0


def test_charges_after_return_and_spends_the_energy_used_away():
    result = solve(request(CHEAP_AFTER_RETURN, c_avail=availability(), e_away=driving()))
    bat = result['batteries'][0]
    assert all(bat['charging_power'][t] == pytest.approx(0, abs=1e-6) for t in AWAY)
    assert sum(bat['charging_power'][:8]) == pytest.approx(0, abs=1e-6)
    assert sum(bat['charging_power'][17:]) > 0
    # the drive comes out of the battery in the steps it happens
    assert bat['state_of_charge'][16] == pytest.approx(20000 - 18000, abs=1e-3)
    assert bat['state_of_charge'][-1] == pytest.approx(60000, abs=1)


def test_driving_energy_is_not_reported_as_a_loss():
    req = request(CHEAP_AFTER_RETURN, c_avail=availability(), e_away=driving())
    result = solve(req)
    bat = result['batteries'][0]
    economic = (- sum(n * p for n, p in zip(result['grid_import'], CHEAP_AFTER_RETURN))
                + sum(result['grid_export']) * 0.00005
                + (bat['state_of_charge'][-1] - 20000 + 18000) * P_A)
    assert result['objective_value'] == pytest.approx(economic, abs=1e-6)


def test_end_goal_is_met_after_an_absence():
    goal = [0.] * T
    goal[-1] = 55000.
    # stored energy is worth less than any import, so only the goal makes it charge
    result = solve(request(CHEAP_WHILE_AWAY, p_a=0.00005, c_avail=availability(), e_away=driving(), s_goal=goal))
    bat = result['batteries'][0]
    assert all(bat['charging_power'][t] == pytest.approx(0, abs=1e-6) for t in AWAY)
    assert bat['state_of_charge'][-1] >= 55000 - 1e-3


def test_goal_and_demand_inside_the_absence_are_ignored():
    goal, demand = [0.] * T, [0.] * T
    goal[12], demand[12] = 59000., 11000.
    result = solve(request(CHEAP_WHILE_AWAY, p_a=0.00005, c_avail=availability(), s_goal=goal, p_demand=demand))
    assert sum(result['batteries'][0]['charging_power']) == pytest.approx(0, abs=1e-6)


def test_shortfall_keeps_an_overdrawn_trip_feasible():
    # far more driving than the battery holds and no chance to charge before leaving
    avail = [0 if t < 17 else 1 for t in range(T)]
    away = [5000. if t < 17 else 0. for t in range(T)]
    result = solve(request(CHEAP_AFTER_RETURN, s_initial=10000., c_avail=avail, e_away=away))
    assert result['status'] == 'Optimal'
    soc = result['batteries'][0]['state_of_charge']
    assert min(soc) >= -1e-6
    assert soc[16] == pytest.approx(0, abs=1e-3)


def test_below_s_min_while_away_costs_nothing():
    # s_min above what is left after the drive: the penalty only applies once the vehicle is back
    result = solve(request(CHEAP_AFTER_RETURN, s_min=10000., c_avail=availability(), e_away=driving()))
    soc = result['batteries'][0]['state_of_charge']
    assert soc[16] == pytest.approx(2000, abs=1e-3)
    assert all(s >= 10000 - 1e-3 for s in soc[17:])


def test_all_available_matches_the_request_without_fields():
    plain = request(CHEAP_WHILE_AWAY)
    explicit = copy.deepcopy(plain)
    explicit['batteries'][0]['c_avail'] = [1] * T
    explicit['batteries'][0]['e_away'] = [0] * T
    a, b = solve(plain), solve(explicit)
    assert a['objective_value'] == pytest.approx(b['objective_value'], abs=1e-9)
    assert a['batteries'][0]['charging_power'] == pytest.approx(b['batteries'][0]['charging_power'], abs=1e-6)


def test_lp_is_unchanged_without_fields(tmp_path):
    """Absent fields must build the very same model, not merely an equivalent one."""
    def lp(**fields):
        optimizer = Optimizer(
            strategy=OptimizationStrategy(charging_strategy='none', discharging_strategy='none'),
            grid=GridConfig(p_max_imp=None, p_max_exp=None, prc_p_exc_imp=None),
            batteries=[BatteryConfig(charge_from_grid=True, discharge_to_grid=False, s_capacity=60000, s_min=5000, s_max=60000,
                                     s_initial=20000, c_min=4140, c_max=11000, d_max=0, p_a=P_A, **fields)],
            time_series=TimeSeriesData(dt=[3600] * T, gt=[500] * T, ft=[0] * T, p_N=CHEAP_WHILE_AWAY, p_E=[0.00005] * T))
        optimizer.create_model()
        path = tmp_path / 'model.lp'
        optimizer.problem.writeLP(str(path))
        return path.read_text()

    assert lp() == lp(c_avail=None, e_away=None)
    assert lp() != lp(c_avail=availability())


def test_return_start_does_not_trigger_the_continuity_stage():
    # cheap before leaving and after returning: one session each, the second opened by the return
    p_N = [0.0004] * T
    p_N[3] = p_N[4] = p_N[20] = p_N[21] = 0.0001
    req = request(p_N, s_initial=10000., c_avail=availability(), e_away=driving(30000.))
    optimizer = Optimizer(
        strategy=OptimizationStrategy(charging_strategy='none', discharging_strategy='none'),
        grid=GridConfig(p_max_imp=None, p_max_exp=None, prc_p_exc_imp=None),
        batteries=[BatteryConfig(discharge_to_grid=False, **req['batteries'][0])],
        time_series=TimeSeriesData(**req['time_series']), eta_c=0.9, eta_d=0.9)
    result = optimizer.solve()
    charging = result['batteries'][0]['charging_power']
    assert sum(charging[:8]) > 0 and sum(charging[17:]) > 0
    assert optimizer.continuity_stage == 'not needed'


@pytest.mark.parametrize('field, values, message', [
    ('c_avail', [1] * (T - 1), 'All time series must have the same length'),
    ('e_away', [0] * (T + 1), 'All time series must have the same length'),
    ('c_avail', [0.5] * T, 'Availability must be 0 or 1 per time step'),
    ('e_away', [-1] * T, 'Energy used away must not be negative'),
])
def test_invalid_availability_is_rejected(field, values, message):
    response = app.test_client().post('/optimize/charge-schedule', json=request(CHEAP_WHILE_AWAY, **{field: values}))
    assert response.status_code == 400
    assert response.get_json()['message'] == message
