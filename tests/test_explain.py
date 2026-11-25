import io
import json
import urllib.error

import pytest

from optimizer import explain
from optimizer.app import app, settings


def test_step_labels_without_timestamps_count_from_the_start(payload):
    assert explain.step_labels(payload['req']['time_series']['dt']) == ['+0:00', '+0:15', '+0:30', '+0:45', '+1:00', '+2:00']


def test_step_labels_are_relative_to_the_first_day():
    stamps = ['2026-10-15T23:45:00.123456789+02:00', '2026-10-16T00:00:00+02:00', '2026-10-17T10:00:00+02:00']
    assert explain.step_labels([900, 900, 3600], stamps) == ['today 23:45', 'tomorrow 00:00', 'Saturday 10:00', 'Saturday 11:00']


def test_digest_describes_batteries_and_lists_every_step(payload):
    text = explain.digest(payload)

    assert 'Now: today 10:00. The plan runs until today 12:00. Status Optimal.' in text
    assert 'Prices in ct/kWh' in text
    assert 'objective' not in text
    assert ('home battery "Home battery": capacity 10.0 kWh, now 50% (5.0 kWh), kept between 0% and 100%, charges up to 5.0 kW, '
            'discharges up to 5.0 kW, may charge from the grid, does not discharge into the grid') in text
    assert 'today 10:30 | 15 | 10.0 | 10.0 | 0.0 | 0.2 | 1.5/0.0 | 1.2/0.0/57' in text
    assert text.endswith('today 11:00 | 60 | 20.0 | 10.0 | 2.0 | 1.0 | 0.0/1.0 | 0.0/0.0/70')

    payload['details']['batteryDetails'] = []
    assert 'home battery "home battery 1": capacity 10.0 kWh' in explain.digest(payload)

    payload['details']['batteryDetails'] = [{'name': 'db:6', 'type': 'battery'}]
    assert 'home battery "home battery with id 6": capacity' in explain.digest(payload)


def test_digest_spells_out_goals_demands_and_limits(payload):
    payload['req']['batteries'][0].update({
        'd_max': 0, 'c_min': 1400, 'c_active': True, 's_max': 8000,
        's_goal': [0, 0, 0, 8000, 0], 'p_demand': [350, 350, 0, 0, 1000],
    })
    payload['details']['batteryDetails'][0] = {'title': 'Carport (i3s)', 'type': 'vehicle', 'capacity': 10}
    payload['details']['demandDetails'] = [{'type': 'home', 'values': []}, {'type': 'heating', 'title': 'Heat pump', 'values': []}]
    payload['req']['grid'] = {'p_max_imp': 11000}
    payload['res']['limit_violations'] = {'grid_import_limit_exceeded': True, 'grid_export_limit_hit': False}
    payload['currency'] = 'GBP'
    text = explain.digest(payload)

    assert ('vehicle "Carport (i3s)": capacity 10.0 kWh, now 50% (5.0 kWh), kept between 0% and 80%, '
            'charges up to 5.0 kW, at least 1.4 kW or not at all, never discharges, may charge from the grid, charging right now, '
            'charging goal 80% by today 11:00, must charge today 10:00 to today 10:30, must charge today 11:00 to today 12:00') in text
    assert 'grid import limited to 11.0 kW.' in text
    assert 'the plan needs more grid import than the limit allows.' in text
    assert 'The household load includes heating "Heat pump".' in text
    assert 'Prices in p/kWh' in text
    assert text.endswith('today 11:00 | 60 | 20.0 | 10.0 | 2.0 | 1.0 | 0.0/1.0 | 0.0/0.0/70')


def test_digest_names_no_level_for_a_vehicle_without_soc(payload):
    payload['req']['batteries'][0].update({'d_max': 0, 's_initial': 3000, 's_goal': [0, 0, 0, 8000, 0]})
    payload['details']['batteryDetails'][0] = {'title': 'Carport', 'type': 'vehicle', 'capacity': 10, 'socUnknown': True}
    text = explain.digest(payload)

    assert 'vehicle "Carport": capacity 10.0 kWh, charge level unknown, charges 2.5 kWh in this plan, charges up to' in text
    assert 'charging goal 5.0 kWh more by today 11:00' in text
    assert 'now 30%' not in text
    assert text.endswith('today 11:00 | 60 | 20.0 | 10.0 | 2.0 | 1.0 | 0.0/1.0 | 0.0/0.0/-')


def test_digest_prices_without_subunit_in_the_currency(payload):
    payload['currency'] = 'HUF'
    assert 'today 10:00 | 15 | 0.300 | 0.100' in explain.digest(payload)


def test_explain_replaces_dashes(payload, monkeypatch):
    monkeypatch.setattr(explain, 'chat', lambda _settings, _messages: ('Sunny — cheap 10:00–13:00.', {}))
    assert explain.explain(payload, settings)[0] == 'Sunny, cheap 10:00-13:00.'


def test_explain_relays_the_digest_in_the_requested_language(payload, configured, monkeypatch, capsys):
    seen = {}

    def fake_chat(_settings, messages):
        seen['messages'] = messages
        return 'Because the price drops at 10:30.', {'total_tokens': 42}

    monkeypatch.setattr(explain, 'chat', fake_chat)
    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 200, response.json
    assert response.json['explanation'] == 'Because the price drops at 10:30.'
    system, user = seen['messages']
    assert system['content'] == explain.SYSTEM_PROMPT + '\nAnswer in the language with IETF tag de.'
    assert 'today 10:00 | 15 | 30.0' in user['content']
    assert '"tokens": 42' in capsys.readouterr().out


@pytest.mark.parametrize('language, expected', [(None, 'en'), ('de-AT', 'de-AT')])
def test_explain_asks_for_the_language(payload, configured, monkeypatch, language, expected):
    seen = {}

    def fake_chat(_settings, messages):
        seen['system'] = messages[0]['content']
        return 'ok', {}

    monkeypatch.setattr(explain, 'chat', fake_chat)
    if language:
        payload['language'] = language
    else:
        del payload['language']

    assert app.test_client().post('/optimize/explain', json=payload).status_code == 200
    assert seen['system'].endswith(f'\nAnswer in the language with IETF tag {expected}.')


def test_explain_reports_model_errors(payload, configured, monkeypatch):
    def failing_chat(_settings, _messages):
        raise explain.ExplainError('model returned 429: slow down')

    monkeypatch.setattr(explain, 'chat', failing_chat)
    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 502
    assert response.json['message'] == 'model returned 429: slow down'


@pytest.mark.parametrize('drop', ['req', 'res'])
def test_explain_requires_request_and_response(payload, configured, drop):
    del payload[drop]
    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 400
    assert drop in response.json['details']


def test_explain_requires_the_plan_status(payload, configured):
    del payload['res']['status']
    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 400
    assert response.json['message'] == 'res.status is a required property'


def test_explain_unconfigured_is_503(payload):
    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 503


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return json.dumps(self.data).encode()


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr(settings, 'anthropic_api_key', 'sk-test')
    monkeypatch.setattr(settings, 'anthropic_model', 'claude-haiku-5-5')


MESSAGES = [{'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'why?'}]


def test_claude_chat_sends_a_messages_request(claude, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen['request'], seen['timeout'] = request, timeout
        return FakeResponse({'content': [{'type': 'thinking', 'thinking': 'hm'}, {'type': 'text', 'text': 'Because '}, {'type': 'text', 'text': 'cheap.'}],
                             'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 5}})

    monkeypatch.setattr(explain.urllib.request, 'urlopen', fake_urlopen)
    text, usage = explain.claude_chat(settings, MESSAGES)

    request = seen['request']
    body = json.loads(request.data)
    assert text == 'Because cheap.'
    assert usage['total_tokens'] == 125
    assert request.full_url == 'https://api.anthropic.com/v1/messages'
    assert request.get_method() == 'POST'
    assert request.get_header('X-api-key') == 'sk-test'
    assert request.get_header('Anthropic-version') == '2023-06-01'
    assert request.get_header('Content-type') == 'application/json'
    assert body['model'] == 'claude-haiku-5-5'
    assert body['max_tokens'] == 1500
    assert body['thinking'] == {'type': 'disabled'}
    assert body['output_config'] == {'effort': 'low'}
    assert body['system'] == [{'type': 'text', 'text': 'be brief', 'cache_control': {'type': 'ephemeral'}}]
    assert body['messages'] == [{'role': 'user', 'content': 'why?'}]


def test_claude_chat_reports_http_errors(claude, monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'slow down'))

    monkeypatch.setattr(explain.urllib.request, 'urlopen', fake_urlopen)
    with pytest.raises(explain.ExplainError, match='model returned 429: slow down'):
        explain.claude_chat(settings, MESSAGES)


def test_claude_chat_reports_timeouts(claude, monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError('timed out')

    monkeypatch.setattr(explain.urllib.request, 'urlopen', fake_urlopen)
    with pytest.raises(explain.ExplainError, match='model unreachable'):
        explain.claude_chat(settings, MESSAGES)


@pytest.mark.parametrize('content', [[], [{'type': 'text', 'text': '  '}], [{'type': 'thinking', 'thinking': 'hm'}]])
def test_claude_chat_reports_an_empty_answer(claude, monkeypatch, content):
    monkeypatch.setattr(explain.urllib.request, 'urlopen', lambda request, timeout: FakeResponse({'content': content, 'stop_reason': 'max_tokens'}))
    with pytest.raises(explain.ExplainError, match=r'no answer \(stop_reason max_tokens, blocks \['):
        explain.claude_chat(settings, MESSAGES)


def test_explain_with_only_the_anthropic_key_answers_through_claude(payload, claude, monkeypatch, capsys):
    monkeypatch.setattr(explain, 'chat', lambda *_: pytest.fail('azure must not be used'))
    monkeypatch.setattr(explain.urllib.request, 'urlopen', lambda request, timeout: FakeResponse(
        {'content': [{'type': 'text', 'text': 'Cheap at 10:30.'}], 'usage': {'input_tokens': 10, 'output_tokens': 4}}))

    response = app.test_client().post('/optimize/explain', json=payload)

    assert response.status_code == 200, response.json
    assert response.json['explanation'] == 'Cheap at 10:30.'
    assert '"tokens": 14' in capsys.readouterr().out


def test_explain_without_the_anthropic_key_uses_azure(payload, configured, monkeypatch):
    monkeypatch.setattr(settings, 'anthropic_api_key', None)
    monkeypatch.setattr(explain, 'claude_chat', lambda *_: pytest.fail('claude must not be used'))
    monkeypatch.setattr(explain, 'chat', lambda _settings, _messages: ('azure answer', {}))

    assert explain.explain(payload, settings)[0] == 'azure answer'
