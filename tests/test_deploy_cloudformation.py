"""Static checks of the CloudFormation templates in deploy/aws/cloudformation.

No AWS calls: the templates are parsed with PyYAML (CloudFormation short tags
become ``{"Fn::X": value}`` mappings, as in the JSON form) and inspected.
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agenticml.server import capacity
from agenticml.server.db.postgres import connection_limits

ROOT = Path(__file__).parents[1]
TEMPLATE_DIR = ROOT / 'deploy/aws/cloudformation'
TEMPLATE_NAMES = ['bootstrap.yaml', 'hosted.yaml']
INLINE_LAMBDA_LIMIT = 4096
# `aws cloudformation deploy` uploads larger templates through an S3 bucket, which the design does not have.
TEMPLATE_SIZE_LIMIT = 51200


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


@pytest.fixture(scope='module')
def hosted():
    return load_template('hosted.yaml')


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
    # The public project address (the ContactEmail default) is the one allowed address.
    text = text.replace('contact@agenticml.xyz', '')
    assert not re.findall(r'[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+', text)


def _find_in_map_key_values(template, key, where):
    """Every value a FindInMap key can take: a literal, an allowed parameter value or an allowed region."""
    if isinstance(key, str):
        return [key]
    assert isinstance(key, dict) and set(key) == {'Ref'}, (
        f'{where}: FindInMap key {key!r} must be a literal or a Ref to a parameter with AllowedValues '
        'or to AWS::Region; extend this test to handle other keys')
    name = key['Ref']
    if name == 'AWS::Region':
        rule = template.get('Rules', {}).get('SupportedRegion')
        assert rule, f'{where}: FindInMap by AWS::Region needs a SupportedRegion rule listing the regions'
        regions = set()
        for assertion in rule['Assertions']:
            contains = assertion['Assert']['Fn::Contains']
            assert contains[1] == {'Ref': 'AWS::Region'}, assertion
            regions.update(contains[0])
        return sorted(regions)
    parameter = template.get('Parameters', {}).get(name)
    assert parameter is not None and 'AllowedValues' in parameter, (
        f'{where}: FindInMap key Ref {name} is not a parameter with AllowedValues, so the lookup cannot be '
        'proved to resolve; give the parameter AllowedValues or handle it in this test')
    return [str(v) for v in parameter['AllowedValues']]


@pytest.mark.parametrize('name', TEMPLATE_NAMES)
def test_every_find_in_map_resolves_for_every_allowed_parameter_value(name):
    # CloudFormation resolves every FindInMap, also in an Fn::If branch that is not taken, so a missing
    # key fails CreateChangeSet ("Unable to get mapping for ...") whatever the conditions say.
    template = load_template(name)
    mappings = template.get('Mappings', {})
    for node in walk(template):
        if not (isinstance(node, dict) and 'Fn::FindInMap' in node):
            continue
        map_name, top_key, second_key = node['Fn::FindInMap']
        assert isinstance(map_name, str) and map_name in mappings, f'{name}: unknown mapping {map_name!r}'
        where = f'{name}: {map_name}'
        for top in _find_in_map_key_values(template, top_key, where):
            assert top in mappings[map_name], f'{where}: no top-level key {top!r}'
            for second in _find_in_map_key_values(template, second_key, where):
                assert second in mappings[map_name][top], f'{where}: no entry for {top!r} / {second!r}'


def test_find_in_map_walker_finds_the_lookups(hosted):
    # Guards the test above against passing on an empty walk.
    found = [n['Fn::FindInMap'][0] for n in walk(hosted) if isinstance(n, dict) and 'Fn::FindInMap' in n]
    assert sorted(found) == ['ConnectionBudget', 'ConnectionsAlarm', 'ElbAccount']


def test_no_email_address_in_deploy_workflow():
    workflow = ROOT / '.github/workflows/deploy.yml'
    if workflow.exists():
        assert '@' not in re.sub(r'[\w./-]+@[0-9a-f]{40}|\[@\]', '', workflow.read_text())


# -------------------------------------------------------------------- bootstrap

def test_bootstrap_parameters(bootstrap):
    assert bootstrap['Transform'] == 'AWS::LanguageExtensions'
    params = bootstrap['Parameters']
    assert set(params) == {'AlertEmails', 'MonthlyBudget', 'CostStep', 'Hostname',
                           'GitHubOidcProviderArn', 'GitHubRepo', 'GitHubSubjectPrefix'}
    assert params['AlertEmails']['Type'] == 'CommaDelimitedList'
    assert 'Default' not in params['AlertEmails']
    assert params['MonthlyBudget']['Default'] == 100
    assert params['CostStep']['Default'] == 10
    assert params['Hostname']['Default'] == 'app.agenticml.xyz'
    assert params['GitHubOidcProviderArn']['Default'] == ''
    assert params['GitHubRepo']['Default'] == 'agenticML/agenticml-lite'
    # The repository uses GitHub's immutable OIDC subjects (owner and repo IDs).
    assert params['GitHubSubjectPrefix']['Default'] == (
        'repo:agenticML@279794012/agenticml-lite@1435335342')


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
        'token.actions.githubusercontent.com:sub': {'Fn::If': [
            'DefaultSubject',
            {'Fn::Sub': 'repo:${GitHubRepo}:environment:hosted'},
            {'Fn::Sub': '${GitHubSubjectPrefix}:environment:hosted'}]},
    }}
    assert bootstrap['Conditions']['DefaultSubject'] == {
        'Fn::Equals': [{'Ref': 'GitHubSubjectPrefix'}, '']}


def test_github_subject_prefix_pattern(bootstrap):
    pattern = re.compile(bootstrap['Parameters']['GitHubSubjectPrefix']['AllowedPattern'])
    assert pattern.fullmatch('')
    assert pattern.fullmatch('repo:agenticML@279794012/agenticml-lite@1435335342')
    for bad in ('repo:agenticML/agenticml-lite', 'repo:*', 'repo:a@1/b@2:environment:x',
                'repo:a@1/b@2*'):
        assert not pattern.fullmatch(bad)


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


def test_deploy_role_ecr_push_is_one_repository(bootstrap):
    stmts = role_statements(bootstrap['Resources']['DeployRole'])
    (push,) = [s for s in stmts if s.get('Sid') == 'EcrPush']
    assert push['Resource'] == {'Fn::GetAtt': 'EcrRepository.Arn'}
    # docker push sends HEAD requests for manifests by digest, which need BatchGetImage.
    assert set(actions_of(push)) == {
        'ecr:BatchCheckLayerAvailability', 'ecr:BatchGetImage', 'ecr:CompleteLayerUpload',
        'ecr:DescribeImages', 'ecr:InitiateLayerUpload', 'ecr:PutImage', 'ecr:UploadLayerPart'}
    # Only EcrLogin (the token call, which has no resource ARN) and EcrPush grant ECR actions.
    ecr_stmts = [s for s in stmts if any(a.startswith('ecr:') for a in actions_of(s))]
    assert sorted(s['Sid'] for s in ecr_stmts) == ['EcrLogin', 'EcrPush']
    (login,) = [s for s in ecr_stmts if s['Sid'] == 'EcrLogin']
    assert actions_of(login) == ['ecr:GetAuthorizationToken'] and login['Resource'] == '*'


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


def test_cloudformation_role_creates_only_the_stacks_service_linked_roles(bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}
    linked = stmts['ServiceLinkedRoles']
    assert actions_of(linked) == ['iam:CreateServiceLinkedRole']
    assert linked['Resource']['Fn::Sub'].endswith(':role/aws-service-role/*')
    # EFS mount targets need the EFS one in a fresh account.
    assert set(linked['Condition']['StringEquals']['iam:AWSServiceName']) == {
        'ecs.amazonaws.com', 'elasticloadbalancing.amazonaws.com', 'rds.amazonaws.com',
        'backup.amazonaws.com', 'elasticfilesystem.amazonaws.com',
        'ecs.application-autoscaling.amazonaws.com'}


def test_cloudformation_role_manages_autoscaling_with_explicit_actions(bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}
    scaling = stmts['Scaling']
    assert scaling['Effect'] == 'Allow' and scaling['Resource'] == '*'
    assert scaling['Condition'] == {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}
    assert set(actions_of(scaling)) == {
        'application-autoscaling:' + a for a in (
            'RegisterScalableTarget', 'DeregisterScalableTarget', 'DescribeScalableTargets',
            'DescribeScalingPolicies', 'DescribeScalingActivities', 'DescribeScheduledActions', 'PutScalingPolicy',
            'DeleteScalingPolicy', 'TagResource', 'UntagResource', 'ListTagsForResource')}
    assert not any('*' in a for a in actions_of(scaling))
    # The scaling policy handler reads the metric: GetMetricData, any resource, this region only.
    metrics = stmts['ScalingMetrics']
    assert metrics['Effect'] == 'Allow' and actions_of(metrics) == ['cloudwatch:GetMetricData']
    assert metrics['Resource'] == '*'
    assert metrics['Condition'] == {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}
    alarms = stmts['ScalingAlarms']
    assert alarms['Effect'] == 'Allow'
    assert set(actions_of(alarms)) == {'cloudwatch:PutMetricAlarm', 'cloudwatch:DeleteAlarms'}
    # The alarms target tracking creates: TargetTracking-<resource id>-AlarmHigh-<id>.
    pattern = fn_sub_text(alarms['Resource']).split('alarm:')[1]
    assert fnmatch.fnmatch('TargetTracking-service/agenticml-hosted/agenticml-hosted-AlarmHigh-0a1b2c3d', pattern)
    assert not fnmatch.fnmatch('TargetTracking-service/other/other-AlarmHigh-0a1b2c3d', pattern)
    # The existing Alarms statement keeps its single agenticml-hosted-* resource.
    assert fn_sub_text(stmts['Alarms']['Resource']).endswith(':alarm:agenticml-hosted-*')
    # The deploy role gains nothing for scaling.
    deploy = role_statements(bootstrap['Resources']['DeployRole'])
    assert not [a for s in deploy for a in actions_of(s) if a.startswith('application-autoscaling:')]


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


# ----------------------------------------------------------------------- hosted

DB_CLASSES = ['db.t4g.micro', 'db.t4g.small', 'db.t4g.medium']
# Conservative estimate of the max_connections left for the application per class: about 70% of RDS's
# LEAST(DBInstanceClassMemory / 9531392, 5000), minus 5 reserved superuser slots, minus about 10 for
# operators, backups and monitoring.
USABLE_CONNECTIONS = {'db.t4g.micro': 64, 'db.t4g.small': 144, 'db.t4g.medium': 300}

def fn_sub_text(value):
    """The template string of an Fn::Sub (plain or with a variable map)."""
    inner = value['Fn::Sub']
    return inner[0] if isinstance(inner, list) else inner


def env_of(hosted):
    (taskdef,) = resources_of(hosted, 'AWS::ECS::TaskDefinition').values()
    (container,) = taskdef['Properties']['ContainerDefinitions']
    return container['Environment']


def alarms_of(hosted):
    return {a['Properties']['AlarmName']: a['Properties']
            for a in resources_of(hosted, 'AWS::CloudWatch::Alarm').values()}


def test_hosted_template_fits_the_deploy_upload_limit():
    assert len((TEMPLATE_DIR / 'hosted.yaml').read_bytes()) < TEMPLATE_SIZE_LIMIT


def test_hosted_parameters(hosted):
    params = hosted['Parameters']
    assert set(params) == {'ImageTag', 'Hostname', 'DesiredCount', 'MaxTaskCount', 'DbInstanceClass', 'MultiAZ',
                           'FirebaseProject', 'FirebaseApiKey', 'OpenSignup', 'AdminEmails', 'LegalBaseUrl',
                           'ContactEmail', 'VpcCidr'}
    assert 'Default' not in params['ImageTag']
    assert params['Hostname']['Default'] == 'app.agenticml.xyz'
    # Lean default (package 24.5): one task; rolling deploys still start the new one first.
    assert params['DesiredCount']['Default'] == 1 and params['DesiredCount']['MinValue'] == 0
    assert params['DesiredCount']['MaxValue'] == 2
    # Autoscaling is off by default: one means no scaling resources.
    assert params['MaxTaskCount']['Type'] == 'String' and params['MaxTaskCount']['Default'] == '1'
    assert params['MaxTaskCount']['AllowedValues'] == ['1', '2', '3', '4']
    # The database class sets the connection ceiling, so it is a closed list.
    assert params['DbInstanceClass']['Type'] == 'String' and params['DbInstanceClass']['Default'] == 'db.t4g.micro'
    assert params['DbInstanceClass']['AllowedValues'] == DB_CLASSES
    assert 'in place' in params['DbInstanceClass']['Description'] and 'max_connections' in params['DbInstanceClass']['Description']
    assert params['MultiAZ']['Default'] == 'false'
    assert params['FirebaseProject']['Default'] == '' and params['FirebaseApiKey']['Default'] == ''
    assert params['OpenSignup']['Default'] == 'false'
    assert params['OpenSignup']['AllowedValues'] == ['true', 'false']
    assert params['AdminEmails']['Default'] == ''
    assert params['AdminEmails']['MaxLength'] == 2048
    assert params['AdminEmails']['AllowedPattern'] == r'^\S*$'
    # Empty by default: nothing is linked from the sign-in page until the owner sets it.
    assert params['LegalBaseUrl']['Default'] == ''
    assert params['LegalBaseUrl']['AllowedPattern'] == r'^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s?#]*)?)?$'
    legal = re.compile(params['LegalBaseUrl']['AllowedPattern'])
    for ok in ('', 'https://docs.example.org/', 'https://docs.example.org:8443/legal/', 'https://a.io'):
        assert legal.match(ok), ok
    for bad in ('http://a.io/', 'https://:443/', 'https://a.io:abc/', 'https://u@a.io/',
                'https://a.io/?x=1', 'https://a.io/#f', 'https://a.io/a b'):
        assert not legal.match(bad), bad
    # The hosted service names its own address (operator mode); the server default is the same one.
    assert params['ContactEmail']['Default'] == 'contact@agenticml.xyz'
    assert params['ContactEmail']['MaxLength'] == 254
    contact = re.compile(params['ContactEmail']['AllowedPattern'])
    for ok in ('', 'off', 'OFF', 'Off', 'contact@agenticml.xyz', 'a.b+c@sub.example.org'):
        assert contact.fullmatch(ok), ok
    for bad in ('a b@x.org', 'a@x.org?cc=b@y.org', 'a@x.org&bcc=b', 'a%40x@y.org', 'a,b@x.org', '"a"@x.org',
                'a@@x.org', 'a@x', 'a@x.org\n', 'of', 'offf', 'mailto:a@x.org'):
        assert not contact.fullmatch(bad), bad
    assert params['VpcCidr']['Default'] == '10.42.0.0/16'
    # No hosted zone, e-mail address or bucket name is a parameter.
    assert not {'HostedZoneId', 'AlarmEmail', 'ObjectsBucketName', 'LogsBucketName'} & set(params)


def test_hosted_has_no_nat_dns_or_certificate_resources(hosted):
    types = {r['Type'] for r in hosted['Resources'].values()}
    assert not {'AWS::EC2::NatGateway', 'AWS::EC2::EIP'} & types
    assert not [t for t in types if t.startswith(('AWS::Route53::', 'AWS::CertificateManager::'))]
    # Alerts come from the bootstrap stack.
    assert not [t for t in types if t.startswith('AWS::SNS::')]
    assert not [n for n in walk(hosted) if isinstance(n, dict) and 'NatGatewayId' in n]


def test_hosted_imports_exactly_the_bootstrap_exports(hosted, bootstrap):
    exported = {o['Export']['Name'] for o in bootstrap['Outputs'].values()}
    imported = {n['Fn::ImportValue'] for n in walk(hosted) if isinstance(n, dict) and 'Fn::ImportValue' in n}
    assert imported == {'agenticml-hosted-bootstrap-EcrRepositoryUri',
                        'agenticml-hosted-bootstrap-CertificateArn',
                        'agenticml-hosted-bootstrap-AlertsTopicArn'}
    assert imported <= exported
    # The execution role's repository ARN uses the name the bootstrap stack gives the repository.
    repo_name = only(bootstrap, 'AWS::ECR::Repository')['Properties']['RepositoryName']
    execution = role_statements(hosted['Resources']['ExecutionRole'])
    (pull,) = [s for s in execution if 'ecr:BatchGetImage' in actions_of(s)]
    assert fn_sub_text(pull['Resource']).endswith(f':repository/{repo_name}')


def test_hosted_database_is_protected(hosted):
    db = only(hosted, 'AWS::RDS::DBInstance')
    props = db['Properties']
    assert db['DeletionPolicy'] == 'Snapshot' and db['UpdateReplacePolicy'] == 'Snapshot'
    assert props['DeletionProtection'] is True
    assert props['StorageEncrypted'] is True
    assert props['PubliclyAccessible'] is False
    assert props['ManageMasterUserPassword'] is True
    assert 'MasterUserPassword' not in props
    assert props['MultiAZ'] == {'Ref': 'MultiAZ'}
    assert (props['Engine'], props['EngineVersion'], props['DBInstanceClass']) == (
        'postgres', '16', {'Ref': 'DbInstanceClass'})
    assert (props['StorageType'], props['AllocatedStorage'], props['MaxAllocatedStorage']) == ('gp3', '20', 100)
    assert props['BackupRetentionPeriod'] == 7
    group = only(hosted, 'AWS::RDS::DBParameterGroup')['Properties']
    assert group['Family'] == 'postgres16' and group['Parameters'] == {'rds.force_ssl': '1'}


def test_hosted_data_stores_are_retained(hosted):
    retained = [only(hosted, 'AWS::EFS::FileSystem'), only(hosted, 'AWS::Backup::BackupVault'),
                hosted['Resources']['ObjectsBucket'], hosted['Resources']['LogsBucket']]
    assert len(resources_of(hosted, 'AWS::S3::Bucket')) == 2
    for resource in retained:
        assert resource['DeletionPolicy'] == 'Retain' and resource['UpdateReplacePolicy'] == 'Retain'


def test_hosted_buckets_are_named_by_cloudformation(hosted):
    # The CloudFormation role may touch only agenticml-hosted-* buckets: CloudFormation's own
    # <stack>-<logical id>-<random> names (stack agenticml-hosted) fit that, explicit names might not.
    for bucket in resources_of(hosted, 'AWS::S3::Bucket').values():
        assert 'BucketName' not in bucket['Properties']
        props = bucket['Properties']
        assert all(props['PublicAccessBlockConfiguration'].values())
        assert props['OwnershipControls']['Rules'] == [{'ObjectOwnership': 'BucketOwnerEnforced'}]
        assert props['BucketEncryption']['ServerSideEncryptionConfiguration'] == [
            {'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
    objects = hosted['Resources']['ObjectsBucket']['Properties']
    assert objects['VersioningConfiguration'] == {'Status': 'Enabled'}
    (rule,) = objects['LifecycleConfiguration']['Rules']
    assert rule['NoncurrentVersionExpiration'] == {'NoncurrentDays': 30}
    assert rule['AbortIncompleteMultipartUpload'] == {'DaysAfterInitiation': 7}
    (rule,) = hosted['Resources']['LogsBucket']['Properties']['LifecycleConfiguration']['Rules']
    assert rule['ExpirationInDays'] == 90
    for policy in resources_of(hosted, 'AWS::S3::BucketPolicy').values():
        deny = [s for s in statements(policy['Properties']['PolicyDocument']) if s['Effect'] == 'Deny']
        assert [s['Condition'] for s in deny] == [{'Bool': {'aws:SecureTransport': 'false'}}]


def test_hosted_logs_bucket_lets_only_the_us_east_2_elb_account_write(hosted):
    assert hosted['Mappings']['ElbAccount'] == {'us-east-2': {'Id': '033677994240'}}
    (rule,) = hosted['Rules']['SupportedRegion']['Assertions']
    assert rule['Assert'] == {'Fn::Contains': [['us-east-2'], {'Ref': 'AWS::Region'}]}
    policy = hosted['Resources']['LogsBucketPolicy']['Properties']['PolicyDocument']
    (allow,) = [s for s in statements(policy) if s['Effect'] == 'Allow']
    assert allow['Action'] == 's3:PutObject'
    assert fn_sub_text(allow['Resource']) == '${LogsBucket.Arn}/alb/AWSLogs/${AWS::AccountId}/*'
    alb = hosted['Resources']['LoadBalancer']
    assert 'LogsBucketPolicy' in alb['DependsOn']
    attributes = {a['Key']: a['Value'] for a in alb['Properties']['LoadBalancerAttributes']}
    assert attributes['access_logs.s3.prefix'] == 'alb'
    assert attributes['access_logs.s3.enabled'] == 'true'


def test_hosted_load_balancer_and_listeners(hosted):
    alb = hosted['Resources']['LoadBalancer']['Properties']
    attributes = {a['Key']: a['Value'] for a in alb['LoadBalancerAttributes']}
    assert attributes['idle_timeout.timeout_seconds'] == '300'
    assert attributes['routing.http.drop_invalid_header_fields.enabled'] == 'true'
    assert alb['IpAddressType'] == 'dualstack' and alb['Scheme'] == 'internet-facing'
    assert alb['Subnets'] == [{'Ref': 'AlbSubnetA'}, {'Ref': 'AlbSubnetB'}]
    group = hosted['Resources']['TargetGroup']['Properties']
    assert (group['Port'], group['Protocol'], group['TargetType']) == (8080, 'HTTP', 'ip')
    assert group['HealthCheckPath'] == '/healthz'
    assert {'Key': 'deregistration_delay.timeout_seconds', 'Value': '120'} in group['TargetGroupAttributes']
    https = hosted['Resources']['HttpsListener']['Properties']
    assert https['Port'] == 443 and https['SslPolicy'] == 'ELBSecurityPolicy-TLS13-1-2-2021-06'
    assert https['Certificates'] == [{'CertificateArn': {
        'Fn::ImportValue': 'agenticml-hosted-bootstrap-CertificateArn'}}]
    (redirect,) = hosted['Resources']['HttpListener']['Properties']['DefaultActions']
    assert redirect['RedirectConfig'] == {'Port': '443', 'Protocol': 'HTTPS', 'StatusCode': 'HTTP_301'}


def test_hosted_subnet_layout(hosted):
    cidr_index = {}
    for name in ('AlbSubnetA', 'AlbSubnetB', 'TaskSubnetA', 'TaskSubnetB', 'DataSubnetA', 'DataSubnetB'):
        cidr = hosted['Resources'][name]['Properties']['CidrBlock']
        index, block = cidr['Fn::Select']
        assert block == {'Fn::Cidr': [{'Ref': 'VpcCidr'}, 32, 8]}
        cidr_index[name] = index
    assert cidr_index == {'AlbSubnetA': 0, 'AlbSubnetB': 1, 'DataSubnetA': 10, 'DataSubnetB': 11,
                          'TaskSubnetA': 20, 'TaskSubnetB': 21}
    # Only the ALB subnets get IPv6.
    with_ipv6 = {k for k, v in resources_of(hosted, 'AWS::EC2::Subnet').items() if 'Ipv6CidrBlock' in v['Properties']}
    assert with_ipv6 == {'AlbSubnetA', 'AlbSubnetB'}


def test_hosted_routes(hosted):
    routes = resources_of(hosted, 'AWS::EC2::Route')
    by_table = {}
    for route in routes.values():
        assert 'NatGatewayId' not in route['Properties']
        assert route['Properties']['GatewayId'] == {'Ref': 'InternetGateway'}
        by_table.setdefault(route['Properties']['RouteTableId']['Ref'], []).append(route)
    assert set(by_table) == {'AlbRouteTable', 'TaskRouteTable'}  # the data route table stays local-only
    assert len(by_table['AlbRouteTable']) == 2 and len(by_table['TaskRouteTable']) == 1
    association = {v['Properties']['SubnetId']['Ref']: v['Properties']['RouteTableId']['Ref']
                   for v in resources_of(hosted, 'AWS::EC2::SubnetRouteTableAssociation').values()}
    assert association == {'AlbSubnetA': 'AlbRouteTable', 'AlbSubnetB': 'AlbRouteTable',
                           'TaskSubnetA': 'TaskRouteTable', 'TaskSubnetB': 'TaskRouteTable',
                           'DataSubnetA': 'DataRouteTable', 'DataSubnetB': 'DataRouteTable'}
    endpoint = only(hosted, 'AWS::EC2::VPCEndpoint')['Properties']
    assert endpoint['VpcEndpointType'] == 'Gateway' and endpoint['RouteTableIds'] == [{'Ref': 'TaskRouteTable'}]


def test_hosted_security_groups(hosted):
    groups = resources_of(hosted, 'AWS::EC2::SecurityGroup')
    assert {g['Properties']['GroupName'] for g in groups.values()} == {
        'agenticml-hosted-alb', 'agenticml-hosted-task', 'agenticml-hosted-rds', 'agenticml-hosted-efs'}

    def ingress(group_id):
        rules = list(groups[group_id]['Properties'].get('SecurityGroupIngress', []))
        for rule in resources_of(hosted, 'AWS::EC2::SecurityGroupIngress').values():
            if rule['Properties']['GroupId'] == {'Ref': group_id}:
                rules.append(rule['Properties'])
        return rules

    # Task group: 8080 from the load balancer group only.
    (rule,) = ingress('TaskSecurityGroup')
    assert (rule['IpProtocol'], rule['FromPort'], rule['ToPort']) == ('tcp', 8080, 8080)
    assert rule['SourceSecurityGroupId'] == {'Ref': 'AlbSecurityGroup'}
    assert set(rule) == {'GroupId', 'IpProtocol', 'FromPort', 'ToPort', 'SourceSecurityGroupId'}
    # Database and file system: only from the task group.
    for group_id, port in (('RdsSecurityGroup', 5432), ('EfsSecurityGroup', 2049)):
        (rule,) = ingress(group_id)
        assert rule == {'IpProtocol': 'tcp', 'FromPort': port, 'ToPort': port,
                        'SourceSecurityGroupId': {'Ref': 'TaskSecurityGroup'}}
    # Load balancer: 80 and 443 from anywhere over IPv4 and IPv6, and egress to the tasks only.
    alb = ingress('AlbSecurityGroup')
    assert sorted((r['FromPort'], r.get('CidrIp') or r.get('CidrIpv6')) for r in alb) == [
        (80, '0.0.0.0/0'), (80, '::/0'), (443, '0.0.0.0/0'), (443, '::/0')]
    assert groups['AlbSecurityGroup']['Properties']['SecurityGroupEgress'] == [{
        'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080,
        'DestinationSecurityGroupId': {'Ref': 'TaskSecurityGroup'}}]
    assert groups['TaskSecurityGroup']['Properties']['SecurityGroupEgress'] == [
        {'IpProtocol': '-1', 'CidrIp': '0.0.0.0/0'}]


def test_hosted_service_runs_in_task_subnets_with_public_ip(hosted):
    service = only(hosted, 'AWS::ECS::Service')['Properties']
    network = service['NetworkConfiguration']['AwsvpcConfiguration']
    assert network['AssignPublicIp'] == 'ENABLED'
    assert network['Subnets'] == [{'Ref': 'TaskSubnetA'}, {'Ref': 'TaskSubnetB'}]
    assert network['SecurityGroups'] == [{'Ref': 'TaskSecurityGroup'}]
    assert service['LaunchType'] == 'FARGATE' and service['PlatformVersion'] == '1.4.0'
    assert service['EnableExecuteCommand'] is True
    # While autoscaling is on, the autoscaler owns the running count.
    assert service['DesiredCount'] == {'Fn::If': ['HasScaling', {'Ref': 'AWS::NoValue'}, {'Ref': 'DesiredCount'}]}
    assert service['HealthCheckGracePeriodSeconds'] == 60
    assert service['DeploymentConfiguration'] == {
        'MinimumHealthyPercent': 100, 'MaximumPercent': 200,
        'DeploymentCircuitBreaker': {'Enable': True, 'Rollback': True}}
    assert service['ServiceName'] == 'agenticml-hosted'
    cluster = hosted['Resources']['Cluster']['Properties']
    assert cluster['ClusterName'] == 'agenticml-hosted'
    # Container Insights costs extra; the alarms use free ALB and RDS metrics.
    assert cluster['ClusterSettings'] == [{'Name': 'containerInsights', 'Value': 'disabled'}]
    # ECS Exec sessions are deliberately not logged: keys and invite links printed
    # in a session would be copied into CloudWatch. CloudTrail records each call.
    assert 'Configuration' not in cluster
    assert not [n for n in walk(hosted) if isinstance(n, dict) and n.get('Namespace') == 'ECS/ContainerInsights']
    assert hosted['Resources']['LogGroup']['Properties'] == {
        'LogGroupName': '/ecs/agenticml-hosted', 'RetentionInDays': 30}


def test_hosted_data_tier_uses_the_data_subnets(hosted):
    data_subnets = [{'Ref': 'DataSubnetA'}, {'Ref': 'DataSubnetB'}]
    assert only(hosted, 'AWS::RDS::DBSubnetGroup')['Properties']['SubnetIds'] == data_subnets
    targets = resources_of(hosted, 'AWS::EFS::MountTarget')
    assert sorted(t['Properties']['SubnetId']['Ref'] for t in targets.values()) == ['DataSubnetA', 'DataSubnetB']
    for target in targets.values():
        assert target['Properties']['SecurityGroups'] == [{'Ref': 'EfsSecurityGroup'}]
    db = only(hosted, 'AWS::RDS::DBInstance')['Properties']
    assert db['VPCSecurityGroups'] == [{'Ref': 'RdsSecurityGroup'}]


def test_hosted_trusted_proxy_is_the_alb_subnets_only(hosted):
    env = {e['Name']: e['Value'] for e in env_of(hosted) if 'Name' in e}
    assert env['AGENTICML_SERVER_TRUSTED_PROXY'] == {'Fn::Join': [',', [
        {'Fn::GetAtt': 'AlbSubnetA.CidrBlock'}, {'Fn::GetAtt': 'AlbSubnetB.CidrBlock'}]]}
    assert env['AGENTICML_SERVER_LIMITS'] == '/etc/agenticml/limits.json'
    assert env['AGENTICML_SERVER_OPEN_SIGNUP'] == {'Ref': 'OpenSignup'}
    assert env['AGENTICML_SERVER_ADMIN_EMAILS'] == {'Ref': 'AdminEmails'}
    assert env['AGENTICML_SERVER_LEGAL_BASE_URL'] == {'Ref': 'LegalBaseUrl'}
    assert env['AGENTICML_SERVER_CONTACT_EMAIL'] == {'Ref': 'ContactEmail'}
    assert env['AGENTICML_SERVER_PUBLIC_URL'] == {'Fn::Sub': 'https://${Hostname}'}
    assert env['AGENTICML_SERVER_OBJECTS'] == {'Fn::Sub': 's3://${ObjectsBucket}/hosted'}
    assert env['AGENTICML_SERVER_METRICS'] == 'cloudwatch'
    assert env['AWS_REGION'] == {'Ref': 'AWS::Region'}
    assert fn_sub_text(env['AGENTICML_SERVER_DATABASE_URL']) == (
        'postgresql://agenticml@${DbInstance.Endpoint.Address}:5432/agenticml'
        '?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem')
    # The password comes from the RDS-managed secret, never from the environment.
    (taskdef,) = resources_of(hosted, 'AWS::ECS::TaskDefinition').values()
    (container,) = taskdef['Properties']['ContainerDefinitions']
    assert container['Secrets'] == [{'Name': 'PGPASSWORD', 'ValueFrom': {
        'Fn::Sub': '${DbInstance.MasterUserSecret.SecretArn}:password::'}}]
    # The server re-reads that same secret when RDS rotates it: the ARN, never the value.
    assert env['AGENTICML_SERVER_DATABASE_PASSWORD_SECRET'] == {
        'Fn::GetAtt': 'DbInstance.MasterUserSecret.SecretArn'}
    assert not [e for e in env_of(hosted) if 'Name' in e and 'PASSWORD' in e['Name']
                and e['Name'] != 'AGENTICML_SERVER_DATABASE_PASSWORD_SECRET']


def test_hosted_rds_postgres_log_is_exported_to_a_30_day_log_group(hosted, bootstrap):
    db = only(hosted, 'AWS::RDS::DBInstance')
    assert db['Properties']['EnableCloudwatchLogsExports'] == ['postgresql']
    assert db['DependsOn'] == 'RdsLogGroup'
    assert hosted['Resources']['RdsLogGroup']['Type'] == 'AWS::Logs::LogGroup'
    assert hosted['Resources']['RdsLogGroup']['Properties'] == {
        'LogGroupName': '/aws/rds/instance/agenticml-hosted/postgresql', 'RetentionInDays': 30}
    assert db['Properties']['DBInstanceIdentifier'] == 'agenticml-hosted'
    # The CloudFormation role manages that group, and already modifies the instance.
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}
    resources = [fn_sub_text(r) for r in as_list(stmts['LogGroups']['Resource'])]
    assert any(r.endswith(':log-group:/aws/rds/instance/agenticml-hosted/*') for r in resources)
    assert any(r.endswith(':log-group:/ecs/agenticml-hosted*') for r in resources)
    assert 'rds:ModifyDBInstance' in {a for s in role_statements(bootstrap['Resources']['CloudFormationRole'])
                                       for a in actions_of(s)}


def test_hosted_firebase_is_conditional_and_open_signup_needs_it(hosted):
    assert hosted['Conditions']['HasFirebase'] == {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'FirebaseProject'}, '']}]}
    conditional = {}
    for entry in env_of(hosted):
        if 'Fn::If' in entry:
            condition, present, absent = entry['Fn::If']
            assert absent == {'Ref': 'AWS::NoValue'}
            if condition == 'HasScaling':
                continue
            assert condition == 'HasFirebase'
            conditional[present['Name']] = present['Value']
    assert conditional == {'AGENTICML_SERVER_FIREBASE_PROJECT': {'Ref': 'FirebaseProject'},
                           'AGENTICML_SERVER_FIREBASE_API_KEY': {'Ref': 'FirebaseApiKey'}}
    # Nothing else in the environment mentions Firebase.
    assert not [e for e in env_of(hosted) if 'Name' in e and 'FIREBASE' in e['Name']]
    rule = hosted['Rules']['OpenSignupNeedsFirebase']
    assert rule['RuleCondition'] == {'Fn::Equals': [{'Ref': 'OpenSignup'}, 'true']}
    (assertion,) = rule['Assertions']
    assert assertion['Assert'] == {'Fn::Not': [{'Fn::Equals': [{'Ref': 'FirebaseProject'}, '']}]}


def test_hosted_image_comes_from_the_imported_repository(hosted):
    (taskdef,) = resources_of(hosted, 'AWS::ECS::TaskDefinition').values()
    props = taskdef['Properties']
    (container,) = props['ContainerDefinitions']
    template, variables = container['Image']['Fn::Sub']
    assert template == '${RepositoryUri}:${ImageTag}'
    assert variables == {'RepositoryUri': {'Fn::ImportValue': 'agenticml-hosted-bootstrap-EcrRepositoryUri'}}
    assert (props['Cpu'], props['Memory'], props['NetworkMode']) == ('512', '1024', 'awsvpc')
    assert props['RuntimePlatform'] == {'CpuArchitecture': 'ARM64', 'OperatingSystemFamily': 'LINUX'}
    assert container['StopTimeout'] == 120
    # An init process is PID 1: it reaps what ECS Exec sessions leave behind and
    # forwards SIGTERM to the server, which drains within StopTimeout.
    assert container['LinuxParameters'] == {'InitProcessEnabled': True}
    (volume,) = props['Volumes']
    assert volume['EFSVolumeConfiguration']['TransitEncryption'] == 'ENABLED'
    assert volume['EFSVolumeConfiguration']['AuthorizationConfig']['IAM'] == 'ENABLED'


def test_hosted_file_system_policy_has_the_three_statements(hosted):
    fs = only(hosted, 'AWS::EFS::FileSystem')['Properties']
    assert fs['Encrypted'] is True
    assert (fs['PerformanceMode'], fs['ThroughputMode']) == ('generalPurpose', 'elastic')
    allow, deny_tls, deny_ap = statements(fs['FileSystemPolicy'])
    assert allow['Principal'] == {'AWS': {'Fn::GetAtt': 'TaskRole.Arn'}}
    assert allow['Effect'] == 'Allow'
    assert set(allow['Action']) == {'elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'}
    assert allow['Condition']['Bool'] == {'aws:SecureTransport': 'true'}
    for statement in (deny_tls, deny_ap):
        assert statement['Effect'] == 'Deny' and statement['Principal'] == '*'
        assert statement['Action'] == 'elasticfilesystem:Client*'
    assert deny_tls['Condition'] == {'Bool': {'aws:SecureTransport': 'false'}}
    assert 'ArnNotLike' in deny_ap['Condition']
    # The access point depends on the file system, so the policy matches access points by pattern.
    point = hosted['Resources']['AccessPoint']['Properties']
    assert point['PosixUser'] == {'Uid': '10001', 'Gid': '10001'}
    assert point['RootDirectory']['Path'] == '/agenticml'
    assert point['RootDirectory']['CreationInfo']['Permissions'] == '0700'


def test_hosted_task_policy_is_scoped(hosted):
    policy = hosted['Resources']['TaskPolicy']['Properties']
    assert policy['Roles'] == [{'Ref': 'TaskRole'}]
    stmts = statements(policy['PolicyDocument'])
    by_action = {}
    for s in stmts:
        for action in actions_of(s):
            by_action.setdefault(action, []).append(s)
    assert fn_sub_text(by_action['s3:GetObject'][0]['Resource']) == '${ObjectsBucket.Arn}/hosted/*'
    list_bucket = by_action['s3:ListBucket']
    assert [s['Condition'] for s in list_bucket] == [{'StringLike': {'s3:prefix': ['hosted/*']}},
                                                     {'Null': {'s3:prefix': 'true'}}]
    efs = by_action['elasticfilesystem:ClientMount'][0]
    assert efs['Resource'] == {'Fn::GetAtt': 'FileSystem.Arn'}
    assert fn_sub_text(efs['Condition']['StringEquals']['elasticfilesystem:AccessPointArn']).endswith(
        ':access-point/${AccessPoint}')
    assert {a for s in stmts for a in actions_of(s) if a.startswith('ssmmessages:')} == {
        'ssmmessages:CreateControlChannel', 'ssmmessages:CreateDataChannel',
        'ssmmessages:OpenControlChannel', 'ssmmessages:OpenDataChannel'}
    # Capacity metrics: only PutMetricData, only in the server's namespace.
    (metrics,) = by_action['cloudwatch:PutMetricData']
    assert actions_of(metrics) == ['cloudwatch:PutMetricData'] and metrics['Effect'] == 'Allow'
    assert metrics['Resource'] == '*'
    assert metrics['Condition'] == {'StringEquals': {'cloudwatch:namespace': capacity.NAMESPACE}}
    # The running server re-reads the RDS-managed password: that one secret, read only.
    (secret,) = by_action['secretsmanager:GetSecretValue']
    assert actions_of(secret) == ['secretsmanager:GetSecretValue'] and secret['Effect'] == 'Allow'
    assert secret['Resource'] == {'Fn::GetAtt': 'DbInstance.MasterUserSecret.SecretArn'}
    assert 'Condition' not in secret
    # Nothing outside S3, EFS, the SSM message channel, CloudWatch metrics and that secret.
    assert {a.split(':')[0] for a in by_action} == {
        's3', 'elasticfilesystem', 'ssmmessages', 'cloudwatch', 'secretsmanager'}


def test_hosted_roles_are_named_inside_the_cloudformation_roles_scope(hosted, bootstrap):
    bootstrap_roles = {'agenticml-hosted-deploy', 'agenticml-hosted-cloudformation'}
    roles = resources_of(hosted, 'AWS::IAM::Role')
    names = {key: role['Properties']['RoleName'] for key, role in roles.items()}
    assert names == {'ExecutionRole': 'agenticml-hosted-execution', 'TaskRole': 'agenticml-hosted-task',
                     'BackupRole': 'agenticml-hosted-backup'}
    assert not bootstrap_roles & set(names.values())
    stmts = role_statements(bootstrap['Resources']['CloudFormationRole'])
    (create,) = [s for s in stmts if s['Effect'] == 'Allow' and 'iam:CreateRole' in actions_of(s)]
    pattern = fn_sub_text(create['Resource']).split(':role/')[1]
    assert all(fnmatch.fnmatch(n, pattern) for n in names.values())
    # Managed policies attached to the backup role are the two the CloudFormation role may attach.
    (attach,) = [s for s in stmts if 'iam:AttachRolePolicy' in actions_of(s) and s['Effect'] == 'Allow']
    allowed = {fn_sub_text(p).split(':iam::aws:policy/')[1] for p in attach['Condition']['ArnEquals']['iam:PolicyARN']}
    used = {fn_sub_text(p).split(':iam::aws:policy/')[1] for p in roles['BackupRole']['Properties']['ManagedPolicyArns']}
    assert used == allowed
    # Trust conditions of the task and execution roles.
    for key in ('ExecutionRole', 'TaskRole'):
        (trust,) = statements(roles[key]['Properties']['AssumeRolePolicyDocument'])
        assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
        assert trust['Condition']['StringEquals'] == {'aws:SourceAccount': {'Ref': 'AWS::AccountId'}}
        assert 'aws:SourceArn' in trust['Condition']['ArnLike']


def test_hosted_resource_names_match_the_cloudformation_role_scope(hosted, bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}

    patterns = [fn_sub_text(r).split('log-group:')[1] for r in as_list(stmts['LogGroups']['Resource'])]
    for key in ('LogGroup', 'RdsLogGroup'):
        log_group = hosted['Resources'][key]['Properties']['LogGroupName']
        assert any(fnmatch.fnmatch(log_group, pattern) for pattern in patterns), key
    alarm_pattern = fn_sub_text(stmts['Alarms']['Resource']).split('alarm:')[1]
    names = alarms_of(hosted)
    assert len(names) == 9
    for name in names:
        assert fnmatch.fnmatch(name, alarm_pattern), name
    bucket_pattern = fn_sub_text(stmts['Buckets']['Resource']).split(':::')[1]
    for logical in ('ObjectsBucket', 'LogsBucket'):
        # CloudFormation's generated name: <stack>-<logical id>-<random>, lowercased.
        assert fnmatch.fnmatch(f'agenticml-hosted-{logical.lower()}-abc123def456', bucket_pattern)
    # Everything the stack creates stays in services the CloudFormation role covers.
    role_services = {a.split(':')[0] for s in stmts.values() if s['Effect'] == 'Allow' for a in actions_of(s)}
    needed = {'AWS::ApplicationAutoScaling': 'application-autoscaling', 'AWS::EC2': 'ec2', 'AWS::ElasticLoadBalancingV2': 'elasticloadbalancing', 'AWS::ECS': 'ecs',
              'AWS::RDS': 'rds', 'AWS::EFS': 'elasticfilesystem', 'AWS::S3': 's3', 'AWS::Logs': 'logs',
              'AWS::CloudWatch': 'cloudwatch', 'AWS::Backup': 'backup', 'AWS::IAM': 'iam'}
    for resource in hosted['Resources'].values():
        assert needed[resource['Type'].rsplit('::', 1)[0]] in role_services, resource['Type']


def test_hosted_autoscaling_is_off_at_one_task_and_creates_two_resources_above(hosted):
    # Scaling needs tasks: DesiredCount 0 always stops the service (no target, no budget).
    assert hosted['Conditions']['HasScaling'] == {'Fn::And': [
        {'Fn::Not': [{'Fn::Equals': [{'Ref': 'MaxTaskCount'}, '1']}]}, {'Condition': 'HasTasks'}]}
    assert hosted['Conditions']['HasTasks'] == {'Fn::Not': [{'Fn::Equals': [{'Ref': 'DesiredCount'}, 0]}]}
    scaling = {k: v for k, v in hosted['Resources'].items() if v['Type'].startswith('AWS::ApplicationAutoScaling::')}
    assert set(scaling) == {'ScalableTarget', 'CpuScalingPolicy'}
    assert all(r['Condition'] == 'HasScaling' for r in scaling.values())
    target = scaling['ScalableTarget']
    # No DependsOn (cfn-lint W3005): the GetAtt on Service.Name in ResourceId already orders it after the service.
    assert target['Type'] == 'AWS::ApplicationAutoScaling::ScalableTarget' and 'DependsOn' not in target
    props = target['Properties']
    assert props == {
        'ServiceNamespace': 'ecs', 'ScalableDimension': 'ecs:service:DesiredCount',
        'ResourceId': {'Fn::Sub': 'service/${Cluster}/${Service.Name}'},
        'MinCapacity': {'Ref': 'DesiredCount'}, 'MaxCapacity': {'Ref': 'MaxTaskCount'}}
    assert 'RoleARN' not in props
    policy = scaling['CpuScalingPolicy']
    assert policy['Type'] == 'AWS::ApplicationAutoScaling::ScalingPolicy'
    assert policy['Properties'] == {
        'PolicyName': 'agenticml-hosted-cpu', 'PolicyType': 'TargetTrackingScaling',
        'ScalingTargetId': {'Ref': 'ScalableTarget'},
        'TargetTrackingScalingPolicyConfiguration': {
            'TargetValue': 60,
            'PredefinedMetricSpecification': {'PredefinedMetricType': 'ECSServiceAverageCPUUtilization'},
            'ScaleOutCooldown': 60, 'ScaleInCooldown': 300}}
    # The service name in the resource id is the one the cluster and service really have.
    assert hosted['Resources']['Cluster']['Properties']['ClusterName'] == 'agenticml-hosted'
    assert hosted['Resources']['Service']['Properties']['ServiceName'] == 'agenticml-hosted'


def evaluate_conditions(hosted, max_tasks, desired):
    """Resolve the hosted template's conditions for MaxTaskCount and DesiredCount (the ones HasScaling reads)."""
    params = {'MaxTaskCount': max_tasks, 'DesiredCount': desired}
    conditions = hosted['Conditions']

    def ev(node):
        (fn, arg), = node.items()
        if fn == 'Condition':
            return ev(conditions[arg])
        if fn == 'Fn::Not':
            return not ev(arg[0])
        if fn == 'Fn::And':
            return all(ev(a) for a in arg)
        if fn == 'Fn::Equals':
            left, right = [params[a['Ref']] if isinstance(a, dict) else a for a in arg]
            return left == right
        raise AssertionError(fn)
    return {name: ev(node) for name, node in conditions.items() if name in ('HasScaling', 'HasTasks')}


def test_hosted_desired_count_zero_stops_the_service_whatever_max_task_count_is(hosted):
    # DesiredCount 0 with a large MaxTaskCount: no scalable target, no budget variable, DesiredCount on the service.
    assert evaluate_conditions(hosted, '3', 0) == {'HasScaling': False, 'HasTasks': False}
    assert evaluate_conditions(hosted, '1', 0) == {'HasScaling': False, 'HasTasks': False}
    assert evaluate_conditions(hosted, '1', 1) == {'HasScaling': False, 'HasTasks': True}
    assert evaluate_conditions(hosted, '3', 1) == {'HasScaling': True, 'HasTasks': True}
    service = only(hosted, 'AWS::ECS::Service')['Properties']
    # The service takes DesiredCount unless HasScaling: so with 0 tasks it always carries DesiredCount 0.
    assert service['DesiredCount'] == {'Fn::If': ['HasScaling', {'Ref': 'AWS::NoValue'}, {'Ref': 'DesiredCount'}]}
    for logical_id in ('ScalableTarget', 'CpuScalingPolicy'):
        assert hosted['Resources'][logical_id]['Condition'] == 'HasScaling'
    # The budget variable and the scaling resources hang on the same condition.
    (entry,) = [e for e in env_of(hosted) if isinstance(e, dict) and 'Fn::If' in e
                and e['Fn::If'][0] == 'HasScaling']
    assert entry['Fn::If'][1]['Name'] == 'AGENTICML_SERVER_DB_CONNECTIONS'
    assert '0 stops the service' in hosted['Parameters']['DesiredCount']['Description']


def test_hosted_connection_budget_fits_the_database_during_a_rolling_deploy(hosted):
    budgets = hosted['Mappings']['ConnectionBudget']
    assert set(budgets) == set(DB_CLASSES) == set(hosted['Parameters']['DbInstanceClass']['AllowedValues'])
    task_counts = hosted['Parameters']['MaxTaskCount']['AllowedValues']
    assert set(task_counts) == {'1', '2', '3', '4'}
    for db_class, usable in USABLE_CONNECTIONS.items():
        # CloudFormation resolves FindInMap even in an Fn::If branch that is not taken, so every class has an
        # entry for every MaxTaskCount (the micro rule still refuses 3 and 4).
        assert set(budgets[db_class]) == set(task_counts)
        for tasks, budget in budgets[db_class].items():
            n = int(tasks)
            # floor(usable / (2n)): the 2 covers the rolling deploy (MaximumPercent 200).
            assert budget == str(usable // (2 * n)), (db_class, tasks)
            assert 2 * n * int(budget) <= usable, (db_class, tasks)
            transaction, outer, inner = connection_limits(int(budget))
            assert transaction + outer + inner + 1 == int(budget)
    assert budgets == {
        'db.t4g.micro': {'1': '32', '2': '16', '3': '10', '4': '8'},
        'db.t4g.small': {'1': '72', '2': '36', '3': '24', '4': '18'},
        'db.t4g.medium': {'1': '150', '2': '75', '3': '50', '4': '37'}}
    # The variable exists only while autoscaling is on, and reads the mapping by class and task count.
    (entry,) = [e for e in env_of(hosted) if isinstance(e, dict) and 'Fn::If' in e
                and e['Fn::If'][0] == 'HasScaling']
    assert entry['Fn::If'] == [
        'HasScaling',
        {'Name': 'AGENTICML_SERVER_DB_CONNECTIONS',
         'Value': {'Fn::FindInMap': ['ConnectionBudget', {'Ref': 'DbInstanceClass'}, {'Ref': 'MaxTaskCount'}]}},
        {'Ref': 'AWS::NoValue'}]


def test_hosted_micro_database_allows_two_tasks_at_most(hosted):
    # A rule keeps MaxTaskCount 3 and 4 off db.t4g.micro (the mapping still has entries for them, so that the lookup resolves).
    rule = hosted['Rules']['MoreTasksNeedABiggerDatabase']
    assert rule['RuleCondition'] == {'Fn::Equals': [{'Ref': 'DbInstanceClass'}, 'db.t4g.micro']}
    (assertion,) = rule['Assertions']
    assert assertion['Assert'] == {'Fn::Contains': [['1', '2'], {'Ref': 'MaxTaskCount'}]}
    assert 'db.t4g.small' in assertion['AssertDescription']


def test_hosted_connections_alarm_follows_the_database_class(hosted):
    # About 80% of the estimated max_connections of each class (usable is that minus operators' room).
    thresholds = hosted['Mappings']['ConnectionsAlarm']
    assert thresholds == {'db.t4g.micro': {'Threshold': '60'}, 'db.t4g.small': {'Threshold': '130'},
                          'db.t4g.medium': {'Threshold': '270'}}
    alarm = alarms_of(hosted)['agenticml-hosted-rds_connections']
    assert alarm['Threshold'] == {'Fn::FindInMap': ['ConnectionsAlarm', {'Ref': 'DbInstanceClass'}, 'Threshold']}
    for db_class, usable in USABLE_CONNECTIONS.items():
        # Below the usable estimate, so the alarm fires before the budgets are exhausted.
        assert int(thresholds[db_class]['Threshold']) <= usable


def test_hosted_alarms_notify_the_imported_topic(hosted):
    topic = {'Fn::ImportValue': 'agenticml-hosted-bootstrap-AlertsTopicArn'}
    alarms = alarms_of(hosted)
    assert set(alarms) == {'agenticml-hosted-alb_5xx', 'agenticml-hosted-unhealthy', 'agenticml-hosted-rds_storage',
                           'agenticml-hosted-rds_cpu', 'agenticml-hosted-healthy_hosts',
                           'agenticml-hosted-latency-p95', 'agenticml-hosted-rds_cpu_credits',
                           'agenticml-hosted-rds_connections', 'agenticml-hosted-ecs_memory'}
    for alarm in alarms.values():
        assert alarm['AlarmActions'] == [topic] and alarm['OKActions'] == [topic]
    summary = {n.removeprefix('agenticml-hosted-'): (a['Namespace'], a['MetricName'], a.get('Statistic'),
                                                     a.get('ExtendedStatistic'), a['ComparisonOperator'],
                                                     a['Threshold'], a['Period'], a['EvaluationPeriods'],
                                                     a['TreatMissingData'])
               for n, a in alarms.items()}
    assert summary == {
        'alb_5xx': ('AWS/ApplicationELB', 'HTTPCode_ELB_5XX_Count', 'Sum', None, 'GreaterThanThreshold', 5, 300, 1, 'notBreaching'),
        'unhealthy': ('AWS/ApplicationELB', 'UnHealthyHostCount', 'Maximum', None, 'GreaterThanOrEqualToThreshold', 1, 60, 5, 'notBreaching'),
        'rds_storage': ('AWS/RDS', 'FreeStorageSpace', 'Minimum', None, 'LessThanThreshold', 2000000000, 300, 1, 'notBreaching'),
        'rds_cpu': ('AWS/RDS', 'CPUUtilization', 'Average', None, 'GreaterThanThreshold', 80, 300, 1, 'notBreaching'),
        'healthy_hosts': ('AWS/ApplicationELB', 'HealthyHostCount', 'Minimum', None, 'LessThanThreshold', {'Ref': 'DesiredCount'}, 60, 5, 'breaching'),
        'latency-p95': ('AWS/ApplicationELB', 'TargetResponseTime', None, 'p95', 'GreaterThanThreshold', 5, 300, 1, 'notBreaching'),
        'rds_cpu_credits': ('AWS/RDS', 'CPUCreditBalance', 'Minimum', None, 'LessThanThreshold', 20, 300, 3, 'notBreaching'),
        'rds_connections': ('AWS/RDS', 'DatabaseConnections', 'Maximum', None, 'GreaterThanThreshold', {'Fn::FindInMap': ['ConnectionsAlarm', {'Ref': 'DbInstanceClass'}, 'Threshold']}, 300, 1, 'notBreaching'),
        'ecs_memory': ('AWS/ECS', 'MemoryUtilization', 'Maximum', None, 'GreaterThanThreshold', 85, 300, 2, 'notBreaching'),
    }
    db = [{'Name': 'DBInstanceIdentifier', 'Value': {'Ref': 'DbInstance'}}]
    assert alarms['agenticml-hosted-rds_cpu_credits']['Dimensions'] == db
    assert alarms['agenticml-hosted-rds_connections']['Dimensions'] == db
    # ECS/ContainerInsights stays off: the memory alarm reads the free service metric.
    assert alarms['agenticml-hosted-ecs_memory']['Dimensions'] == [
        {'Name': 'ClusterName', 'Value': {'Ref': 'Cluster'}},
        {'Name': 'ServiceName', 'Value': {'Fn::GetAtt': 'Service.Name'}}]
    # healthy_hosts (fewer healthy targets than tasks wanted for 5 minutes) only
    # exists while DesiredCount is not 0, so scaling to 0 does not leave it in
    # ALARM. A new MaxValue needs a review of the threshold wording.
    assert hosted['Parameters']['DesiredCount']['MaxValue'] == 2
    assert hosted['Conditions']['HasTasks'] == {'Fn::Not': [{'Fn::Equals': [{'Ref': 'DesiredCount'}, 0]}]}
    conditions = {r['Properties']['AlarmName']: r.get('Condition')
                  for r in resources_of(hosted, 'AWS::CloudWatch::Alarm').values()}
    assert conditions.pop('agenticml-hosted-healthy_hosts') == 'HasTasks'
    # Created after the service is stable: no false alarm while the first tasks start.
    assert hosted['Resources']['HealthyHostsAlarm']['DependsOn'] == 'Service'
    healthy = alarms['agenticml-hosted-healthy_hosts']
    assert healthy['Dimensions'] == [
        {'Name': 'LoadBalancer', 'Value': {'Fn::GetAtt': 'LoadBalancer.LoadBalancerFullName'}},
        {'Name': 'TargetGroup', 'Value': {'Fn::GetAtt': 'TargetGroup.TargetGroupFullName'}}]
    assert not any(conditions.values())


def test_bootstrap_dashboard_matches_the_hosted_names(bootstrap, hosted):
    dashboard = only(bootstrap, 'AWS::CloudWatch::Dashboard')['Properties']
    assert dashboard['DashboardName'] == 'agenticml-hosted'
    template = fn_sub_text(dashboard['DashboardBody'])
    assert set(re.findall(r'\$\{([^}]*)\}', template)) == {'AWS::Region'}
    body = json.loads(template.replace('${AWS::Region}', 'eu-north-1'))
    metrics = [m for w in body['widgets'] for m in w['properties']['metrics']]
    assert {w['properties']['region'] for w in body['widgets']} == {'eu-north-1'}
    # The names the dashboard refers to are the ones hosted.yaml defines.
    props = lambda type_: only(hosted, type_)['Properties']
    cluster, service = props('AWS::ECS::Cluster')['ClusterName'], props('AWS::ECS::Service')['ServiceName']
    database = props('AWS::RDS::DBInstance')['DBInstanceIdentifier']
    assert (cluster, service, database) == ('agenticml-hosted',) * 3
    ecs = [m for m in metrics if m[0] == 'AWS/ECS']
    assert {m[1] for m in ecs} == {'CPUUtilization', 'MemoryUtilization'}
    assert all(m[2:6] == ['ClusterName', cluster, 'ServiceName', service] for m in ecs)
    rds = [m for m in metrics if m[0] == 'AWS/RDS']
    assert {m[1] for m in rds} == {'DatabaseConnections', 'CPUUtilization', 'CPUCreditBalance'}
    assert all(m[2:4] == ['DBInstanceIdentifier', database] for m in rds)
    # The namespace is the one the task policy allows.
    (put,) = [s for s in statements(props('AWS::IAM::Policy')['PolicyDocument'])
              if 'cloudwatch:PutMetricData' in actions_of(s)]
    namespace = put['Condition']['StringEquals']['cloudwatch:namespace']
    own = [m for m in metrics if m[0] not in ('AWS/ECS', 'AWS/RDS')]
    assert namespace == capacity.NAMESPACE
    assert {m[0] for m in own} == {capacity.NAMESPACE}
    assert {m[1] for m in own} == set(capacity.METRIC_NAMES)
    assert {m[-1]['stat'] for m in own if m[1] == 'Latency'} == {'p50', 'p95', 'p99'}
    # Nothing in the hosted stack imports or refers to the dashboard.
    assert 'Dashboard' not in (TEMPLATE_DIR / 'hosted.yaml').read_text()


def test_bootstrap_dashboard_statistics_and_periods(bootstrap):
    dashboard = only(bootstrap, 'AWS::CloudWatch::Dashboard')['Properties']
    body = json.loads(fn_sub_text(dashboard['DashboardBody']).replace('${AWS::Region}', 'eu-north-1'))
    shown = {}
    for widget in body['widgets']:
        props = widget['properties']
        for line in props['metrics']:
            options = line[-1] if isinstance(line[-1], dict) else {}
            stat = options.get('stat', props.get('stat'))
            period = options.get('period', props.get('period'))
            shown.setdefault((line[0], line[1]), set()).add((stat, period))
    ns = capacity.NAMESPACE
    expected = {(ns, name): {('Sum', 60)} for name in ('Requests', 'Errors4xx', 'Errors5xx', 'Rejected')}
    expected.update({(ns, 'Latency'): {('p50', 60), ('p95', 60), ('p99', 60)}})
    expected.update({(ns, name): {('Maximum', 60), ('Sum', 60)} for name in ('InFlightPeak', 'DbConnectionsPeak')})
    expected.update({(ns, name): {('Maximum', 3600)} for name in (
        'Accounts', 'ActiveUsers7d', 'WorkspacesActive', 'StoredBytes', 'UploadBytesToday')})
    expected[('AWS/RDS', 'CPUCreditBalance')] = {('Minimum', 300)}
    for key, want in expected.items():
        assert shown[key] == want, key
    assert {name for (namespace, name) in shown if namespace == ns} == set(capacity.METRIC_NAMES)


def test_hosted_backup_plan(hosted):
    plan = only(hosted, 'AWS::Backup::BackupPlan')['Properties']['BackupPlan']
    (rule,) = plan['BackupPlanRule']
    assert rule['ScheduleExpression'] == 'cron(0 5 * * ? *)'
    assert rule['Lifecycle'] == {'DeleteAfterDays': 35}
    assert rule['TargetBackupVault'] == {'Ref': 'BackupVault'}
    selection = only(hosted, 'AWS::Backup::BackupSelection')['Properties']['BackupSelection']
    assert selection['Resources'] == [{'Fn::GetAtt': 'FileSystem.Arn'}, {'Fn::GetAtt': 'DbInstance.DBInstanceArn'}]


def test_hosted_outputs(hosted):
    assert set(hosted['Outputs']) == {'ServiceUrl', 'AlbDnsName', 'ClusterName', 'ServiceName', 'LogGroup',
                                      'ObjectsBucket', 'LogsBucket', 'EfsId', 'DbInstanceId',
                                      'TaskSecurityGroup', 'TaskSubnets'}
    assert hosted['Outputs']['AlbDnsName']['Value'] == {'Fn::GetAtt': 'LoadBalancer.DNSName'}


# ------------------------------------------------------------------ workflows

def load_workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def run_blocks(workflow):
    return [step['run'] for job in workflow['jobs'].values() for step in job.get('steps', []) if 'run' in step]


def test_deploy_workflow_runs_the_hosted_template_through_the_cloudformation_role():
    workflow = load_workflow('deploy.yml')
    text = (ROOT / '.github/workflows/deploy.yml').read_text()
    job = workflow['jobs']['deploy']
    assert job['environment'] == 'hosted'
    assert workflow['permissions'] == {'contents': 'read', 'id-token': 'write'}
    assert workflow['concurrency'] == {'group': 'hosted-deploy', 'cancel-in-progress': False}
    assert 'deploy/aws/cloudformation/hosted.yaml' in text
    assert '--role-arn' in text
    assert 'CAPABILITY_NAMED_IAM' in text
    assert 'agenticml/lite-server' in text
    assert 'terraform' not in text.lower()
    assert 'swap_image' not in text


def test_deploy_workflow_run_blocks_take_values_from_env_only():
    workflow = load_workflow('deploy.yml')
    blocks = run_blocks(workflow)
    assert blocks
    for block in blocks:
        assert '${{' not in block, block


def test_deploy_workflow_passes_the_admin_emails_secret_as_a_parameter_override():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (deploy,) = [s for s in steps if 'ADMIN_EMAILS' in s.get('env', {})]
    assert deploy['env']['ADMIN_EMAILS'] == '${{ secrets.HOSTED_ADMIN_EMAILS }}'
    assert 'AdminEmails="$ADMIN_EMAILS"' in deploy['run']
    assert '${{' not in deploy['run']


def _deploy_step():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (deploy,) = [s for s in steps if 'ADMIN_EMAILS' in s.get('env', {})]
    return deploy


def _check_step():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (check,) = [s for s in steps if s.get('name') == 'Check required variables']
    return check


def test_deploy_workflow_masks_each_admin_email_before_the_deploy():
    run = _deploy_step()['run']
    assert '::add-mask::' in run
    assert run.index('::add-mask::') < run.index('aws cloudformation deploy')


def test_deploy_workflow_reads_the_admin_emails_variable_only_as_a_flag():
    text = (ROOT / '.github/workflows/deploy.yml').read_text()
    uses = re.findall(r'vars\.HOSTED_ADMIN_EMAILS.*', text)
    assert uses
    for use in uses:
        assert re.match(r"vars\.HOSTED_ADMIN_EMAILS != ''", use), use


def _run_bash(block, env):
    result = subprocess.run(['bash', '-c', block], env=env, capture_output=True, text=True)
    return result.returncode, result.stdout


@pytest.mark.skipif(shutil.which('bash') is None, reason='bash is not installed')
@pytest.mark.parametrize('flag, fails', [('true', True), ('false', False)])
def test_deploy_workflow_check_step_refuses_a_leftover_admin_emails_variable(flag, fails):
    env = {'PATH': os.environ['PATH'], 'DEPLOY_ROLE_ARN': 'arn:aws:iam::123456789012:role/deploy',
           'CFN_ROLE_ARN': 'arn:aws:iam::123456789012:role/cfn', 'ADMIN_EMAILS_VARIABLE_SET': flag}
    code, stdout = _run_bash(_check_step()['run'], env)
    if fails:
        assert code != 0
        assert 'HOSTED_ADMIN_EMAILS' in stdout
    else:
        assert code == 0


@pytest.mark.skipif(shutil.which('bash') is None, reason='bash is not installed')
def test_deploy_workflow_masking_prints_one_mask_per_address():
    run = _deploy_step()['run']
    masking = run[:run.index('overrides=(')]
    base = {'PATH': os.environ['PATH']}
    code, stdout = _run_bash(masking, {**base, 'ADMIN_EMAILS': 'a@example.com,b@example.com'})
    assert code == 0
    assert stdout.splitlines() == ['::add-mask::a@example.com', '::add-mask::b@example.com']
    code, stdout = _run_bash(masking, {**base, 'ADMIN_EMAILS': ''})
    assert code == 0
    assert stdout == ''


def test_deploy_workflow_passes_the_legal_base_url_variable_as_a_parameter_override():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (deploy,) = [s for s in steps if 'LEGAL_BASE_URL' in s.get('env', {})]
    assert deploy['env']['LEGAL_BASE_URL'] == '${{ vars.HOSTED_LEGAL_BASE_URL }}'
    assert 'LegalBaseUrl="$LEGAL_BASE_URL"' in deploy['run']
    assert '${{' not in deploy['run']


def test_deploy_workflow_fails_early_on_missing_role_variables_and_prints_events():
    workflow = load_workflow('deploy.yml')
    steps = workflow['jobs']['deploy']['steps']
    assert 'AWS_DEPLOY_ROLE_ARN' in steps[0]['run'] and 'AWS_CFN_ROLE_ARN' in steps[0]['run']
    events = [s for s in steps if 'describe-stack-events' in s.get('run', '')]
    assert len(events) == 1 and events[0]['if'].startswith('failure()')


def test_deploy_workflow_timeout_covers_a_first_create():
    assert load_workflow('deploy.yml')['jobs']['deploy']['timeout-minutes'] == 60


def test_deploy_workflow_only_deploys_commits_on_the_dispatching_ref():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout'))
    assert checkout['with']['fetch-depth'] == 0
    assert checkout['with']['persist-credentials'] is False
    guard = [i for i, s in enumerate(steps) if 'git merge-base --is-ancestor' in s.get('run', '')]
    assert len(guard) == 1
    creds = [i for i, s in enumerate(steps) if s.get('uses', '').startswith('aws-actions/configure-aws-credentials')]
    assert len(creds) == 1
    assert steps.index(checkout) < guard[0] < creds[0]
    assert steps[guard[0]]['env']['INPUT_REF'] == '${{ inputs.ref }}'


def test_deploy_workflow_runs_on_main_pushes_and_on_dispatch():
    workflow = load_workflow('deploy.yml')
    # PyYAML reads the bare `on` key as True.
    triggers = workflow[True]
    assert set(triggers) == {'push', 'workflow_dispatch'}
    assert triggers['push']['branches'] == ['main']
    # Docs and the bootstrap template (deployed by hand) do not queue a deploy.
    assert triggers['push']['paths'] == [
        'src/**', 'pyproject.toml', 'deploy/aws/**', '!deploy/aws/**/*.md',
        '!deploy/aws/cloudformation/bootstrap.yaml', '.github/workflows/deploy.yml']
    count = triggers['workflow_dispatch']['inputs']['desired_count']
    assert count['type'] == 'choice' and count['default'] == '1' and set(count['options']) == {'0', '1', '2'}


def test_deploy_workflow_desired_count_comes_from_the_input_not_a_variable():
    text = (ROOT / '.github/workflows/deploy.yml').read_text()
    assert 'HOSTED_DESIRED_COUNT' not in text
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (deploy,) = [s for s in steps if 'DESIRED_COUNT' in s.get('env', {})]
    assert deploy['env']['DESIRED_COUNT'] == '${{ inputs.desired_count }}'
    # Push runs (no input) leave the parameter out, so the stack keeps its count.
    assert 'if [ -n "$DESIRED_COUNT" ]; then\n  overrides+=(DesiredCount="$DESIRED_COUNT")' in deploy['run']
    assert '--parameter-overrides "${overrides[@]}"' in deploy['run']
    # The final check compares against the service's own desired count.
    (check,) = [s for s in steps if 'runningCount' in s.get('run', '')]
    assert "'services[0].[runningCount,desiredCount]'" in check['run']


def test_deploy_workflow_builds_an_arm64_image_with_an_arch_tag():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    qemu = [i for i, s in enumerate(steps) if s.get('uses', '').startswith('docker/setup-qemu-action@')]
    build = [i for i, s in enumerate(steps) if 'docker build' in s.get('run', '')]
    assert len(qemu) == 1 and len(build) == 1 and qemu[0] < build[0]
    assert steps[qemu[0]]['with']['platforms'] == 'arm64'
    run = steps[build[0]]['run']
    assert 'docker build --platform linux/arm64 ' in run
    assert 'tag="$(git rev-parse HEAD)-arm64"' in run
    assert 'imageTag="$tag"' in run and 'IMAGE_TAG=$tag' in run
    dockerfile = (ROOT / 'deploy/aws/Dockerfile').read_text()
    assert 'FROM --platform=$BUILDPLATFORM python:' in dockerfile


def test_deploy_check_job_lints_templates_instead_of_terraform():
    workflow = load_workflow('test.yml')
    steps = workflow['jobs']['deploy-check']['steps']
    text = (ROOT / '.github/workflows/test.yml').read_text()
    assert 'terraform' not in text.lower()
    runs = '\n'.join(s.get('run', '') for s in steps)
    assert re.search(r'cfn-lint==\d+\.\d+\.\d+', runs)
    assert 'cfn-lint deploy/aws/cloudformation/*.yaml' in runs


def test_nothing_under_deploy_aws_mentions_terraform():
    for path in (ROOT / 'deploy/aws').rglob('*'):
        if path.is_file():
            assert 'terraform' not in path.read_text(errors='ignore').lower(), path


USES_RE = re.compile(r'^\s*-?\s*uses:\s*(\S+?)@(\S+)[ \t]*(#.*)?$', re.M)


@pytest.mark.parametrize('path', sorted((ROOT / '.github/workflows').glob('*.yml')), ids=lambda p: p.name)
def test_workflow_actions_are_pinned_to_commit_shas(path):
    text = path.read_text()
    uses = USES_RE.findall(text)
    assert uses, path
    # Every `uses:` line is one the pattern understood, so none slips past the check.
    assert len(uses) == len(re.findall(r'^\s*-?\s*uses:', text, re.M))
    for action, ref, comment in uses:
        assert re.fullmatch(r'[0-9a-f]{40}', ref), f'{path.name}: {action}@{ref} is not a commit SHA'
        assert re.fullmatch(r'#\s*v\d+(\.\d+){0,2}', comment.strip()), f'{path.name}: {action} has no tag comment'
    assert 'pinned to full commit SHAs' in text


def test_ecs_exec_policy_is_stated_in_the_runbook_and_threat_model():
    readme = ' '.join((ROOT / 'deploy/aws/README.md').read_text().split())
    threat = ' '.join((ROOT / 'docs/THREAT_MODEL.md').read_text().split())
    assert 'deliberately not logged' in readme and 'CloudTrail records each `ExecuteCommand` call' in readme
    assert 'InitProcessEnabled' in readme
    assert 'deliberately not logged' in threat and 'CloudWatch' in threat
    assert 'directly as PID 1' not in (ROOT / 'deploy/aws/entrypoint.sh').read_text()


def test_deploy_workflow_max_task_count_is_an_optional_input_passed_through_env():
    workflow = load_workflow('deploy.yml')
    option = workflow[True]['workflow_dispatch']['inputs']['max_task_count']
    assert option['type'] == 'choice' and option['default'] == 'keep'
    assert option['options'] == ['keep', '1', '2', '3', '4']
    steps = workflow['jobs']['deploy']['steps']
    (deploy,) = [s for s in steps if 'MAX_TASK_COUNT' in s.get('env', {})]
    assert deploy['env']['MAX_TASK_COUNT'] == '${{ inputs.max_task_count }}'
    # Push runs (no input) and "keep" leave the parameter out, so the stack keeps its value.
    assert ('if [ -n "$MAX_TASK_COUNT" ] && [ "$MAX_TASK_COUNT" != keep ]; then\n'
            '  overrides+=(MaxTaskCount="$MAX_TASK_COUNT")') in deploy['run']
    assert '${{' not in deploy['run']
    assert len([s for s in steps if 'describe-stack-events' in s.get('run', '')]) == 1


def test_deploy_workflow_final_check_follows_the_stacks_scaling_parameters():
    steps = load_workflow('deploy.yml')['jobs']['deploy']['steps']
    (check,) = [s for s in steps if 'runningCount' in s.get('run', '')]
    run = check['run']
    assert '${{' not in run and 'describe-stack-events' not in run
    # The stack's own MaxTaskCount and DesiredCount parameters (resolved values) decide.
    assert "ParameterKey=='MaxTaskCount'" in run and "ParameterKey=='DesiredCount'" in run
    assert run.count('describe-stacks') >= 3
    # Autoscaling on (MaxTaskCount above 1 and DesiredCount above 0): up to 30 x 10 s, running == desired >= minimum.
    assert 'if [ "$max_tasks" -gt 1 ] && [ "$min_tasks" -gt 0 ]; then' in run
    assert 'attempts=30' in run and 'attempts=12' in run and 'sleep 10' in run
    assert 'if [ "$running" = "$desired" ]; then' in run
    assert 'if [ "$scaling" = true ] && [ "$desired" -ge "$min_tasks" ]; then' in run
    # Otherwise the service must run exactly DesiredCount; a left-over scale-out fails with the fix to run.
    assert 'if [ "$scaling" = false ] && [ "$desired" = "$min_tasks" ]; then' in run
    assert 'if [ "$scaling" = false ] && [ "$desired" != "$min_tasks" ]; then' in run
    assert '::error::' in run and 'aws ecs update-service' in run and '--desired-count $min_tasks' in run
    assert 'redeploy with desired_count' in run
    # Values come from the job env only.
    env = load_workflow('deploy.yml')['jobs']['deploy']['env']
    assert {'STACK_NAME', 'ECS_CLUSTER', 'ECS_SERVICE'} <= set(env)
    assert len([s for s in steps if 'describe-stack-events' in s.get('run', '')]) == 1


def test_deploy_role_can_describe_the_hosted_stack_for_the_final_check(bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['DeployRole']) if 'Sid' in s}
    assert 'cloudformation:DescribeStacks' in actions_of(stmts['HostedStack'])
    assert stmts['HostedStack']['Resource']['Fn::Sub'].endswith(':stack/agenticml-hosted/*')
