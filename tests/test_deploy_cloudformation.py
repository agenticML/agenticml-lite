"""Static checks of the CloudFormation templates in deploy/aws/cloudformation.

No AWS calls: the templates are parsed with PyYAML (CloudFormation short tags
become ``{"Fn::X": value}`` mappings, as in the JSON form) and inspected.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
TEMPLATE_DIR = ROOT / 'deploy/aws/cloudformation'
# Part 24.4b adds 'hosted.yaml' here and its own assertions below.
TEMPLATE_NAMES = ['bootstrap.yaml']
INLINE_LAMBDA_LIMIT = 4096


class CfnLoader(yaml.SafeLoader):
    pass


def _short_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    key = suffix if suffix in ('Ref', 'Condition') else f'Fn::{suffix}'
    return {key: value}


CfnLoader.add_multi_constructor('!', _short_tag)


def load_template(name):
    return yaml.load((TEMPLATE_DIR / name).read_text(), Loader=CfnLoader)


def resources_of(template, type_):
    return {k: v for k, v in template['Resources'].items()
            if isinstance(v, dict) and v.get('Type') == type_}


def only(template, type_):
    found = resources_of(template, type_)
    assert len(found) == 1, f'expected one {type_}, found {sorted(found)}'
    return next(iter(found.values()))


def walk(node):
    """Yield every mapping and list nested in node, node included."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def as_list(value):
    return value if isinstance(value, list) else [value]


def statements(document):
    return as_list(document['Statement'])


def role_statements(role):
    out = []
    for policy in role['Properties'].get('Policies', []):
        out += statements(policy['PolicyDocument'])
    return out


def actions_of(statement):
    return as_list(statement.get('Action', []))


def inline_lambda_source(template, logical_id='CostStepsFunction'):
    return template['Resources'][logical_id]['Properties']['Code']['ZipFile']


@pytest.fixture(scope='module')
def bootstrap():
    return load_template('bootstrap.yaml')


# ---------------------------------------------------------------- all templates

@pytest.mark.parametrize('name', TEMPLATE_NAMES)
def test_no_iam_wildcard_actions(name):
    template = load_template(name)
    for node in walk(template):
        if isinstance(node, dict) and 'Action' in node and 'Effect' in node:
            for action in actions_of(node):
                assert action != '*', node
                assert not action.lower().startswith('iam:*'), node


@pytest.mark.parametrize('name', TEMPLATE_NAMES)
def test_no_email_address_in_template(name):
    text = (TEMPLATE_DIR / name).read_text()
    assert not re.findall(r'[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+', text)


def test_no_email_address_in_deploy_workflow():
    workflow = ROOT / '.github/workflows/deploy.yml'
    if workflow.exists():
        assert '@' not in re.sub(r'actions/[\w-]+@v\d+|aws-actions/[\w-]+@v\d+', '', workflow.read_text())


# -------------------------------------------------------------------- bootstrap

def test_bootstrap_parameters(bootstrap):
    assert bootstrap['Transform'] == 'AWS::LanguageExtensions'
    params = bootstrap['Parameters']
    assert set(params) == {'AlertEmails', 'MonthlyBudget', 'CostStep', 'Hostname',
                           'GitHubOidcProviderArn', 'GitHubRepo'}
    assert params['AlertEmails']['Type'] == 'CommaDelimitedList'
    assert 'Default' not in params['AlertEmails']
    assert params['MonthlyBudget']['Default'] == 100
    assert params['CostStep']['Default'] == 10
    assert params['Hostname']['Default'] == 'app.agenticml.xyz'
    assert params['GitHubOidcProviderArn']['Default'] == ''
    assert params['GitHubRepo']['Default'] == 'agenticML/agenticml-lite'


def test_oidc_provider_only_when_arn_not_given(bootstrap):
    provider = only(bootstrap, 'AWS::IAM::OIDCProvider')
    assert provider['Condition'] == 'CreateOidcProvider'
    assert provider['Properties']['Url'] == 'https://token.actions.githubusercontent.com'
    assert provider['Properties']['ClientIdList'] == ['sts.amazonaws.com']
    assert bootstrap['Conditions']['CreateOidcProvider'] == {
        'Fn::Equals': [{'Ref': 'GitHubOidcProviderArn'}, '']}


def test_ecr_repository(bootstrap):
    repo = only(bootstrap, 'AWS::ECR::Repository')
    props = repo['Properties']
    assert props['RepositoryName'] == 'agenticml/lite-server'
    assert props['ImageTagMutability'] == 'IMMUTABLE'
    assert props['ImageScanningConfiguration']['ScanOnPush'] is True
    assert repo['DeletionPolicy'] == 'Retain'
    assert '"countNumber": 30' in props['LifecyclePolicy']['LifecyclePolicyText']


def test_deploy_role_trust_is_one_repo_environment(bootstrap):
    role = bootstrap['Resources']['DeployRole']
    assert role['Properties']['RoleName'] == 'agenticml-hosted-deploy'
    (trust,) = statements(role['Properties']['AssumeRolePolicyDocument'])
    assert trust['Effect'] == 'Allow'
    assert trust['Action'] == 'sts:AssumeRoleWithWebIdentity'
    assert list(trust['Principal']) == ['Federated']
    assert trust['Condition'] == {'StringEquals': {
        'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
        'token.actions.githubusercontent.com:sub': {
            'Fn::Sub': 'repo:${GitHubRepo}:environment:hosted'},
    }}


def test_deploy_role_passrole_only_to_cloudformation_role(bootstrap):
    stmts = role_statements(bootstrap['Resources']['DeployRole'])
    passing = [s for s in stmts if 'iam:PassRole' in actions_of(s)]
    assert len(passing) == 1
    (stmt,) = passing
    assert actions_of(stmt) == ['iam:PassRole']
    assert stmt['Resource'] == {'Fn::GetAtt': 'CloudFormationRole.Arn'}
    assert stmt['Condition'] == {'StringEquals': {'iam:PassedToService': 'cloudformation.amazonaws.com'}}
    # The deploy role gets no other IAM, RDS or direct ECS write access.
    for s in stmts:
        for action in actions_of(s):
            assert action == 'iam:PassRole' or not action.startswith(('iam:', 'rds:', 'ec2:'))
            if action.startswith('ecs:'):
                assert action == 'ecs:DescribeServices'
            assert 'ExecuteCommand' not in action


def test_deploy_role_cloudformation_scope(bootstrap):
    stmts = role_statements(bootstrap['Resources']['DeployRole'])
    for s in stmts:
        cfn = [a for a in actions_of(s) if a.startswith('cloudformation:')]
        if not cfn or cfn == ['cloudformation:ValidateTemplate']:
            continue
        assert s['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:cloudformation:${AWS::Region}'
                                            ':${AWS::AccountId}:stack/agenticml-hosted/*'}
    assert {a for s in stmts for a in actions_of(s) if a.startswith('cloudformation:')} == {
        'cloudformation:CreateChangeSet', 'cloudformation:DeleteChangeSet',
        'cloudformation:DescribeChangeSet', 'cloudformation:DescribeStackEvents',
        'cloudformation:DescribeStacks', 'cloudformation:ExecuteChangeSet',
        'cloudformation:GetTemplateSummary', 'cloudformation:ValidateTemplate'}


def test_cloudformation_role(bootstrap):
    role = bootstrap['Resources']['CloudFormationRole']
    assert role['Properties']['RoleName'] == 'agenticml-hosted-cloudformation'
    (trust,) = statements(role['Properties']['AssumeRolePolicyDocument'])
    assert trust['Principal'] == {'Service': 'cloudformation.amazonaws.com'}
    assert trust['Condition'] == {'StringEquals': {'aws:SourceAccount': {'Ref': 'AWS::AccountId'}}}
    stmts = role_statements(role)
    services = {a.split(':')[0] for s in stmts if s['Effect'] == 'Allow' for a in actions_of(s)}
    assert not {'route53', 'sns', 'lambda'} & services
    # Every IAM write except service-linked roles is limited to agenticml-hosted-* roles.
    for s in stmts:
        if s['Effect'] != 'Allow' or not any(a.startswith('iam:') for a in actions_of(s)):
            continue
        resource = s['Resource']['Fn::Sub']
        assert resource.endswith(':role/agenticml-hosted-*') or ':role/aws-service-role/' in resource, s
    # ... and it cannot touch the two bootstrap roles.
    (deny,) = [s for s in stmts if s['Effect'] == 'Deny']
    assert {r['Fn::Sub'].rsplit('/', 1)[1] for r in deny['Resource']} == {
        'agenticml-hosted-deploy', 'agenticml-hosted-cloudformation'}
    assert {'iam:PassRole', 'iam:PutRolePolicy', 'iam:AttachRolePolicy'} <= set(actions_of(deny))
    # Policies can only be attached from the two AWS Backup managed ones.
    (attach,) = [s for s in stmts if 'iam:AttachRolePolicy' in actions_of(s) and s['Effect'] == 'Allow']
    assert all('AWSBackupServiceRolePolicyFor' in p['Fn::Sub']
               for p in attach['Condition']['ArnEquals']['iam:PolicyARN'])


def test_cloudformation_role_covers_subnet_cidrs_and_managed_secret_rotation(bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}
    # The hosted stack's ALB subnets get IPv6 CIDRs, which CloudFormation sets with these calls.
    assert {'ec2:AssociateSubnetCidrBlock', 'ec2:DisassociateSubnetCidrBlock'} <= set(actions_of(stmts['Network']))
    # RDS ManageMasterUserPassword needs the caller to hold these three.
    assert {'secretsmanager:CreateSecret', 'secretsmanager:RotateSecret', 'secretsmanager:TagResource'} <= set(
        actions_of(stmts['ManagedDatabaseSecret']))


def test_certificate_is_dns_validated_without_hosted_zone(bootstrap):
    cert = only(bootstrap, 'AWS::CertificateManager::Certificate')
    props = cert['Properties']
    assert props['DomainName'] == {'Ref': 'Hostname'}
    assert props['ValidationMethod'] == 'DNS'
    assert cert['DeletionPolicy'] == 'Retain'
    assert 'DomainValidationOptions' not in props
    assert not [n for n in walk(bootstrap) if isinstance(n, dict) and 'HostedZoneId' in n]
    assert not resources_of(bootstrap, 'AWS::Route53::RecordSet')


def test_alerts_topic_and_policy(bootstrap):
    topic = only(bootstrap, 'AWS::SNS::Topic')
    assert topic['Properties']['TopicName'] == 'agenticml-alerts'
    assert 'KmsMasterKeyId' not in topic['Properties']
    policy = only(bootstrap, 'AWS::SNS::TopicPolicy')
    stmts = statements(policy['Properties']['PolicyDocument'])
    by_principal = {}
    for s in stmts:
        assert s['Effect'] == 'Allow' and s['Action'] == 'sns:Publish'
        assert s['Resource'] == {'Ref': 'AlertsTopic'}
        by_principal[tuple(s['Principal'].items())[0][1] if 'Service' in s['Principal'] else 'root'] = s
    assert set(by_principal) == {'budgets.amazonaws.com', 'cloudwatch.amazonaws.com', 'root'}
    for service in ('budgets.amazonaws.com', 'cloudwatch.amazonaws.com'):
        assert by_principal[service]['Condition'] == {
            'StringEquals': {'aws:SourceAccount': {'Ref': 'AWS::AccountId'}}}


def test_alert_emails_become_one_subscription_each(bootstrap):
    loops = [v for k, v in bootstrap['Resources'].items() if k.startswith('Fn::ForEach::')]
    assert len(loops) == 1
    identifier, collection, body = loops[0]
    assert collection == {'Ref': 'AlertEmails'}
    (logical, sub), = body.items()
    assert logical == 'AlertEmail&{%s}' % identifier
    assert sub['Type'] == 'AWS::SNS::Subscription'
    assert sub['Properties'] == {'TopicArn': {'Ref': 'AlertsTopic'}, 'Protocol': 'email',
                                 'Endpoint': {'Ref': identifier}}


def test_budget_excludes_credits_and_refunds(bootstrap):
    budget = only(bootstrap, 'AWS::Budgets::Budget')
    assert budget['DependsOn'] == 'AlertsTopicPolicy'
    spec = budget['Properties']['Budget']
    assert spec['BudgetType'] == 'COST' and spec['TimeUnit'] == 'MONTHLY'
    assert spec['BudgetLimit'] == {'Amount': {'Ref': 'MonthlyBudget'}, 'Unit': 'USD'}
    assert spec['CostTypes']['IncludeCredit'] is False
    assert spec['CostTypes']['IncludeRefund'] is False
    notifications = budget['Properties']['NotificationsWithSubscribers']
    assert sorted((n['Notification']['NotificationType'], n['Notification']['ComparisonOperator'],
                   n['Notification']['Threshold'], n['Notification']['ThresholdType'])
                  for n in notifications) == [('ACTUAL', 'GREATER_THAN', 100, 'PERCENTAGE'),
                                              ('FORECASTED', 'GREATER_THAN', 100, 'PERCENTAGE')]
    for n in notifications:
        assert n['Subscribers'] == [{'SubscriptionType': 'SNS', 'Address': {'Ref': 'AlertsTopic'}}]


def test_inline_cost_function_fits_the_zipfile_limit(bootstrap):
    source = inline_lambda_source(bootstrap)
    assert len(source) <= INLINE_LAMBDA_LIMIT
    compile(source, 'cost_steps_inline', 'exec')


def test_cost_function_wiring(bootstrap):
    fn = bootstrap['Resources']['CostStepsFunction']['Properties']
    assert fn['Runtime'] == 'python3.12' and fn['Handler'] == 'index.handler'
    assert fn['MemorySize'] == 128 and fn['Timeout'] == 60
    assert fn['Environment']['Variables'] == {
        'TOPIC_ARN': {'Ref': 'AlertsTopic'}, 'STEP': {'Ref': 'CostStep'},
        'BUDGET': {'Ref': 'MonthlyBudget'}, 'STATE_PARAM': '/agenticml/cost-alerts/state'}
    log_group = only(bootstrap, 'AWS::Logs::LogGroup')['Properties']
    assert log_group['LogGroupName'] == '/aws/lambda/agenticml-cost-steps'
    assert log_group['RetentionInDays'] == 14
    rule = only(bootstrap, 'AWS::Events::Rule')['Properties']
    assert rule['ScheduleExpression'] == 'cron(0 6 * * ? *)'
    assert rule['Targets'][0]['Arn'] == {'Fn::GetAtt': 'CostStepsFunction.Arn'}
    alarm = only(bootstrap, 'AWS::CloudWatch::Alarm')['Properties']
    assert (alarm['Namespace'], alarm['MetricName'], alarm['Statistic']) == ('AWS/Lambda', 'Errors', 'Sum')
    assert alarm['Period'] == 86400 and alarm['Threshold'] == 1
    assert alarm['ComparisonOperator'] == 'GreaterThanOrEqualToThreshold'
    assert alarm['AlarmActions'] == [{'Ref': 'AlertsTopic'}]


def test_cost_function_role_is_least_privilege(bootstrap):
    stmts = role_statements(bootstrap['Resources']['CostStepsRole'])
    allowed = {a: s['Resource'] for s in stmts for a in actions_of(s)}
    assert set(allowed) == {'logs:CreateLogStream', 'logs:PutLogEvents', 'ce:GetCostAndUsage',
                            'ce:GetCostForecast', 'ssm:GetParameter', 'ssm:PutParameter', 'sns:Publish'}
    assert allowed['ce:GetCostAndUsage'] == '*' == allowed['ce:GetCostForecast']
    for action in ('ssm:GetParameter', 'ssm:PutParameter'):
        assert allowed[action]['Fn::Sub'].endswith(':parameter/agenticml/cost-alerts/state')
    assert allowed['sns:Publish'] == {'Ref': 'AlertsTopic'}


def test_bootstrap_outputs_are_exported(bootstrap):
    outputs = bootstrap['Outputs']
    assert set(outputs) == {'DeployRoleArn', 'CloudFormationRoleArn', 'EcrRepositoryUri',
                            'CertificateArn', 'AlertsTopicArn'}
    for key, output in outputs.items():
        assert output['Export']['Name'] == f'agenticml-hosted-bootstrap-{key}'
