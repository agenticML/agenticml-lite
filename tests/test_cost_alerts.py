"""Run the inline cost-step Lambda from bootstrap.yaml against fake AWS clients.

The test injects a stub ``boto3`` whose ``client`` returns fakes, so no real
client is created and no AWS call is ever made.
"""
import datetime as dt
import json
import sys
import types

import pytest

from test_deploy_cloudformation import inline_lambda_source, load_template

PARAM = '/agenticml/cost-alerts/state'
TOPIC = 'arn:aws:sns:us-east-2:111111111111:agenticml-alerts'
CREDIT_REFUND_FILTER = {'Not': {'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit', 'Refund']}}}


class FakeCE:
    class exceptions:
        class DataUnavailableException(Exception):
            pass

    def __init__(self, usage, forecast=None):
        self.usage = usage  # {(start, end): {service: amount}}
        self.forecast = forecast  # amount, or None -> DataUnavailableException
        self.queries, self.forecasts = [], []
        self.pages = 1

    def get_cost_and_usage(self, **kw):
        self.queries.append(kw)
        period = (kw['TimePeriod']['Start'], kw['TimePeriod']['End'])
        assert period[0] < period[1], 'Cost Explorer rejects empty periods'
        items = list(self.usage[period].items())
        page = int(kw.get('NextPageToken', 0))
        chunk = items[page::self.pages]
        resp = {'ResultsByTime': [{'Groups': [
            {'Keys': [svc], 'Metrics': {'UnblendedCost': {'Amount': str(amt), 'Unit': 'USD'}}}
            for svc, amt in chunk]}]}
        if page + 1 < self.pages:
            resp['NextPageToken'] = str(page + 1)
        return resp

    def get_cost_forecast(self, **kw):
        self.forecasts.append(kw)
        if self.forecast is None:
            raise self.exceptions.DataUnavailableException('too early')
        return {'Total': {'Amount': str(self.forecast), 'Unit': 'USD'}}


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, state=None):
        self.value = None if state is None else json.dumps(state)
        self.puts = []

    def get_parameter(self, Name):
        assert Name == PARAM
        if self.value is None:
            raise self.exceptions.ParameterNotFound(Name)
        return {'Parameter': {'Value': self.value}}

    def put_parameter(self, **kw):
        self.puts.append(kw)
        assert kw['Name'] == PARAM and kw['Type'] == 'String' and kw['Overwrite'] is True
        self.value = kw['Value']

    @property
    def state(self):
        return json.loads(self.value)


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, **kw):
        assert kw['TopicArn'] == TOPIC
        self.sent.append(kw)


@pytest.fixture
def lam(monkeypatch):
    """Load the inline source as a module with a stub boto3 and the function's environment."""
    clients = {}
    created = []

    def client(name, **kw):
        created.append((name, kw))
        return clients[name]

    stub = types.ModuleType('boto3')
    stub.client = client
    monkeypatch.setitem(sys.modules, 'boto3', stub)
    for key, value in {'TOPIC_ARN': TOPIC, 'STEP': '10', 'BUDGET': '100', 'STATE_PARAM': PARAM}.items():
        monkeypatch.setenv(key, value)
    module = types.ModuleType('cost_steps')
    exec(compile(inline_lambda_source(load_template('bootstrap.yaml')), 'cost_steps_inline', 'exec'),
         module.__dict__)

    def run(day, ce, ssm, sns=None):
        sns = sns or FakeSNS()
        clients.update(ce=ce, ssm=ssm, sns=sns)
        monkeypatch.setattr(module, 'today', lambda: dt.date.fromisoformat(day))
        module.handler({}, None)
        return sns

    run.created = created
    return run


def usage(**periods):
    """usage(a_b={'EC2': 1}) with keys like '2026-11-01_2026-11-10'."""
    return {tuple(k.split('_')): v for k, v in periods.items()}


NOV10 = '2026-11-01_2026-11-10'


def test_first_run_under_one_step_sends_nothing_and_saves_state(lam):
    ce, ssm = FakeCE(usage(**{NOV10: {'EC2': 4.5, 'S3': 1.0}})), FakeSSM()
    sns = lam('2026-11-10', ce, ssm)
    assert sns.sent == []
    assert ssm.state == {'month': '2026-11', 'step': 0}
    assert len(ssm.puts) == 1
    assert ce.forecasts == []


def test_crossing_one_step_sends_one_message(lam):
    ssm = FakeSSM()
    sns = lam('2026-11-10', FakeCE(usage(**{NOV10: {'EC2': 9.99}})), ssm)
    assert sns.sent == [] and ssm.state['step'] == 0
    sns = lam('2026-11-10', FakeCE(usage(**{NOV10: {'EC2': 10.0}}), forecast=80), ssm)
    (msg,) = sns.sent
    assert msg['Subject'] == 'AWS spend passed $10 this month ($10.00)'
    assert msg['TopicArn'] == TOPIC
    lines = msg['Message'].splitlines()
    assert lines[0] == 'Month to date: $10.00'
    assert lines[1] == 'Budget: $100 (10% used)'
    assert lines[2] == 'Forecast for month end: $90.00'
    assert '$10.00  EC2' in lines
    assert lines[-1].startswith('https://console.aws.amazon.com/cost-management/')
    assert ssm.state == {'month': '2026-11', 'step': 1}


def test_several_steps_in_one_day_send_one_message(lam):
    ssm = FakeSSM({'month': '2026-11', 'step': 1})
    sns = lam('2026-11-10', FakeCE(usage(**{NOV10: {'EC2': 34.0}}), forecast=50), ssm)
    (msg,) = sns.sent
    assert msg['Subject'] == 'AWS spend passed $30 this month ($34.00)'
    assert ssm.state == {'month': '2026-11', 'step': 3}


def test_same_step_again_sends_nothing(lam):
    ssm = FakeSSM({'month': '2026-11', 'step': 3})
    sns = lam('2026-11-11', FakeCE(usage(**{'2026-11-01_2026-11-11': {'EC2': 39.5}})), ssm)
    assert sns.sent == []
    assert ssm.puts == []  # nothing changed, nothing written


def test_month_rollover_sends_previous_month_summary_and_resets_state(lam):
    ssm = FakeSSM({'month': '2026-10', 'step': 2})
    ce = FakeCE(usage(**{'2026-10-01_2026-11-01': {'EC2': 20.0, 'S3': 5.5},
                         '2026-11-01_2026-11-05': {'EC2': 3.0}}))
    sns = lam('2026-11-05', ce, ssm)
    (msg,) = sns.sent
    assert msg['Subject'] == 'AWS spend for 2026-10: $25.50'
    assert msg['Message'].splitlines() == ['Total for 2026-10: $25.50', '$20.00  EC2', '$5.50  S3']
    assert ssm.state == {'month': '2026-11', 'step': 0}


def test_rollover_then_new_month_already_past_a_step_sends_both(lam):
    ssm = FakeSSM({'month': '2026-10', 'step': 2})
    ce = FakeCE(usage(**{'2026-10-01_2026-11-01': {'EC2': 25.0},
                         '2026-11-01_2026-11-05': {'EC2': 12.0}}), forecast=40)
    sns = lam('2026-11-05', ce, ssm)
    assert [m['Subject'] for m in sns.sent] == [
        'AWS spend for 2026-10: $25.00', 'AWS spend passed $10 this month ($12.00)']
    assert ssm.state == {'month': '2026-11', 'step': 1}


class Untouchable:
    """A client that fails the test if anything is called on it."""

    class exceptions:
        pass

    def __getattr__(self, name):
        raise AssertionError(f'unexpected call: {name}')


@pytest.mark.parametrize('state', [None, {'month': '2026-10', 'step': 4}])
def test_first_of_month_does_nothing(lam, state):
    ce, ssm = FakeCE({}), FakeSSM(state)
    before = ssm.value
    sns = lam('2026-11-01', ce, ssm)
    assert ce.queries == [] and ce.forecasts == []
    assert sns.sent == []
    assert ssm.puts == [] and ssm.value == before


def test_first_of_month_touches_no_client_at_all(lam):
    lam('2026-11-01', Untouchable(), Untouchable(), Untouchable())


def test_rollover_on_the_second_saves_state_right_after_the_summary(lam):
    events = []

    class Ssm(FakeSSM):
        def put_parameter(self, **kw):
            events.append(('put', json.loads(kw['Value'])))
            super().put_parameter(**kw)

    class Sns(FakeSNS):
        def publish(self, **kw):
            events.append(('publish', kw['Subject']))
            super().publish(**kw)

    class Ce(FakeCE):
        def get_cost_and_usage(self, **kw):
            events.append(('query', kw['TimePeriod']['Start']))
            return super().get_cost_and_usage(**kw)

    ssm = Ssm({'month': '2026-10', 'step': 4})
    ce = Ce(usage(**{'2026-10-01_2026-11-01': {'EC2': 45.0}, '2026-11-01_2026-11-02': {'EC2': 1.0}}))
    sns = lam('2026-11-02', ce, ssm, Sns())
    assert [m['Subject'] for m in sns.sent] == ['AWS spend for 2026-10: $45.00']
    assert events == [
        ('query', '2026-10-01'),
        ('publish', 'AWS spend for 2026-10: $45.00'),
        ('put', {'month': '2026-11', 'step': 0}),
        ('query', '2026-11-01'),
    ]
    assert len(ssm.puts) == 1  # nothing changed after the rollover write
    assert ssm.state == {'month': '2026-11', 'step': 0}


def test_error_after_the_rollover_summary_does_not_resend_it_next_run(lam):
    class Broken(FakeCE):
        def get_cost_and_usage(self, **kw):
            if kw['TimePeriod']['Start'] == '2026-11-01':
                raise RuntimeError('Cost Explorer is down')
            return super().get_cost_and_usage(**kw)

    ssm = FakeSSM({'month': '2026-10', 'step': 2})
    broken = Broken(usage(**{'2026-10-01_2026-11-01': {'EC2': 25.0}}))
    sns = FakeSNS()
    with pytest.raises(RuntimeError):
        lam('2026-11-02', broken, ssm, sns)
    assert [m['Subject'] for m in sns.sent] == ['AWS spend for 2026-10: $25.00']
    assert ssm.state == {'month': '2026-11', 'step': 0}
    # The next run finds the new month in the state and sends no second summary.
    ce = FakeCE(usage(**{'2026-11-01_2026-11-03': {'EC2': 2.0}}))
    sns = lam('2026-11-03', ce, ssm, sns)
    assert len(sns.sent) == 1


def test_older_state_month_is_summarised_not_the_last_month(lam):
    ssm = FakeSSM({'month': '2026-09', 'step': 1})
    ce = FakeCE(usage(**{'2026-09-01_2026-10-01': {'EC2': 11.0}, '2026-11-01_2026-11-03': {'EC2': 1.0}}))
    sns = lam('2026-11-03', ce, ssm)
    assert [m['Subject'] for m in sns.sent] == ['AWS spend for 2026-09: $11.00']


def test_forecast_unavailable_omits_the_forecast_line(lam):
    ssm = FakeSSM({'month': '2026-11', 'step': 0})
    ce = FakeCE(usage(**{'2026-11-01_2026-11-02': {'EC2': 10.5}}), forecast=None)
    sns = lam('2026-11-02', ce, ssm)
    (msg,) = sns.sent
    assert 'Forecast' not in msg['Message']
    assert 'Month to date: $10.50' in msg['Message']
    assert ssm.state['step'] == 1
    assert ce.forecasts[0]['TimePeriod'] == {'Start': '2026-11-02', 'End': '2026-12-01'}


def test_breakdown_is_sorted_and_drops_sub_cent_services(lam):
    ssm = FakeSSM({'month': '2026-11', 'step': 0})
    services = {'Tiny': 0.004, 'Small': 0.01, 'Big': 30.0, 'Mid': 12.0}
    sns = lam('2026-11-10', FakeCE(usage(**{NOV10: services}), forecast=60), ssm)
    (msg,) = sns.sent
    rows = msg['Message'].split('By service:\n')[1].splitlines()[:-1]
    assert rows == ['$30.00  Big', '$12.00  Mid', '$0.01  Small']
    assert 'Tiny' not in msg['Message']
    assert msg['Subject'] == 'AWS spend passed $40 this month ($42.01)'


def test_credits_and_refunds_are_filtered_out_of_every_query(lam):
    ssm = FakeSSM({'month': '2026-10', 'step': 0})
    ce = FakeCE(usage(**{'2026-10-01_2026-11-01': {'EC2': 1.0}, '2026-11-01_2026-11-05': {'EC2': 11.0}}),
                forecast=30)
    lam('2026-11-05', ce, ssm)
    assert len(ce.queries) == 2 and len(ce.forecasts) == 1
    for kw in ce.queries:
        assert kw['Filter'] == CREDIT_REFUND_FILTER
        assert kw['Granularity'] == 'MONTHLY' and kw['Metrics'] == ['UnblendedCost']
        assert kw['GroupBy'] == [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
    assert ce.forecasts[0]['Filter'] == CREDIT_REFUND_FILTER


def test_cost_explorer_client_uses_us_east_1(lam):
    lam('2026-11-10', FakeCE(usage(**{NOV10: {'EC2': 1.0}})), FakeSSM())
    assert ('ce', {'region_name': 'us-east-1'}) in lam.created


def test_paged_results_are_summed(lam):
    ce = FakeCE(usage(**{NOV10: {'A': 4.0, 'B': 5.0, 'C': 6.0}}), forecast=20)
    ce.pages = 2
    sns = lam('2026-11-10', ce, FakeSSM())
    (msg,) = sns.sent
    assert msg['Subject'] == 'AWS spend passed $10 this month ($15.00)'
    assert len(ce.queries) == 2


def test_other_errors_propagate_and_state_is_not_saved(lam):
    class Broken(FakeCE):
        def get_cost_and_usage(self, **kw):
            raise RuntimeError('Cost Explorer is not enabled')

    ssm = FakeSSM({'month': '2026-11', 'step': 1})
    with pytest.raises(RuntimeError):
        lam('2026-11-10', Broken({}), ssm)
    assert ssm.puts == []
