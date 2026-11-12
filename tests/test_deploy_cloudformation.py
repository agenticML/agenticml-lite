"""Static checks of the CloudFormation templates in deploy/aws/cloudformation.

No AWS calls: the templates are parsed with PyYAML (CloudFormation short tags
become ``{"Fn::X": value}`` mappings, as in the JSON form) and inspected.
"""
import fnmatch
import re
from pathlib import Path

import pytest
import yaml

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
    assert not re.findall(r'[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+', text)


def test_no_email_address_in_deploy_workflow():
    workflow = ROOT / '.github/workflows/deploy.yml'
    if workflow.exists():
        assert '@' not in re.sub(r'(?:aws-actions|actions|docker)/[\w-]+@v\d+|\[@\]', '', workflow.read_text())


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


def test_cloudformation_role_creates_only_the_stacks_service_linked_roles(bootstrap):
    stmts = {s['Sid']: s for s in role_statements(bootstrap['Resources']['CloudFormationRole']) if 'Sid' in s}
    linked = stmts['ServiceLinkedRoles']
    assert actions_of(linked) == ['iam:CreateServiceLinkedRole']
    assert linked['Resource']['Fn::Sub'].endswith(':role/aws-service-role/*')
    # EFS mount targets need the EFS one in a fresh account.
    assert set(linked['Condition']['StringEquals']['iam:AWSServiceName']) == {
        'ecs.amazonaws.com', 'elasticloadbalancing.amazonaws.com', 'rds.amazonaws.com',
        'backup.amazonaws.com', 'elasticfilesystem.amazonaws.com'}


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
    assert set(params) == {'ImageTag', 'Hostname', 'DesiredCount', 'MultiAZ', 'FirebaseProject',
                           'FirebaseApiKey', 'OpenSignup', 'VpcCidr'}
    assert 'Default' not in params['ImageTag']
    assert params['Hostname']['Default'] == 'app.agenticml.xyz'
    # Lean default (package 24.5): one task; rolling deploys still start the new one first.
    assert params['DesiredCount']['Default'] == 1 and params['DesiredCount']['MinValue'] == 0
    assert params['MultiAZ']['Default'] == 'false'
    assert params['FirebaseProject']['Default'] == '' and params['FirebaseApiKey']['Default'] == ''
    assert params['OpenSignup']['Default'] == 'false'
    assert params['OpenSignup']['AllowedValues'] == ['true', 'false']
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
    assert (props['Engine'], props['EngineVersion'], props['DBInstanceClass']) == ('postgres', '16', 'db.t4g.micro')
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
    assert service['DesiredCount'] == {'Ref': 'DesiredCount'}
    assert service['HealthCheckGracePeriodSeconds'] == 60
    assert service['DeploymentConfiguration'] == {
        'MinimumHealthyPercent': 100, 'MaximumPercent': 200,
        'DeploymentCircuitBreaker': {'Enable': True, 'Rollback': True}}
    assert service['ServiceName'] == 'agenticml-hosted'
    cluster = hosted['Resources']['Cluster']['Properties']
    assert cluster['ClusterName'] == 'agenticml-hosted'
    # Container Insights costs extra; the alarms use free ALB and RDS metrics.
    assert cluster['ClusterSettings'] == [{'Name': 'containerInsights', 'Value': 'disabled'}]
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
    assert env['AGENTICML_SERVER_PUBLIC_URL'] == {'Fn::Sub': 'https://${Hostname}'}
    assert env['AGENTICML_SERVER_OBJECTS'] == {'Fn::Sub': 's3://${ObjectsBucket}/hosted'}
    assert env['AWS_REGION'] == {'Ref': 'AWS::Region'}
    assert fn_sub_text(env['AGENTICML_SERVER_DATABASE_URL']) == (
        'postgresql://agenticml@${DbInstance.Endpoint.Address}:5432/agenticml'
        '?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem')
    # The password comes from the RDS-managed secret, never from the environment.
    (taskdef,) = resources_of(hosted, 'AWS::ECS::TaskDefinition').values()
    (container,) = taskdef['Properties']['ContainerDefinitions']
    assert container['Secrets'] == [{'Name': 'PGPASSWORD', 'ValueFrom': {
        'Fn::Sub': '${DbInstance.MasterUserSecret.SecretArn}:password::'}}]
    assert not [e for e in env_of(hosted) if 'Name' in e and 'PASSWORD' in e['Name']]


def test_hosted_firebase_is_conditional_and_open_signup_needs_it(hosted):
    assert hosted['Conditions']['HasFirebase'] == {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'FirebaseProject'}, '']}]}
    conditional = {}
    for entry in env_of(hosted):
        if 'Fn::If' in entry:
            condition, present, absent = entry['Fn::If']
            assert condition == 'HasFirebase' and absent == {'Ref': 'AWS::NoValue'}
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
    # Nothing outside S3, EFS and the SSM message channel.
    assert {a.split(':')[0] for a in by_action} == {'s3', 'elasticfilesystem', 'ssmmessages'}


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

    log_group = hosted['Resources']['LogGroup']['Properties']['LogGroupName']
    assert fnmatch.fnmatch(log_group, fn_sub_text(stmts['LogGroups']['Resource']).split('log-group:')[1])
    alarm_pattern = fn_sub_text(stmts['Alarms']['Resource']).split('alarm:')[1]
    names = alarms_of(hosted)
    assert len(names) == 6
    for name in names:
        assert fnmatch.fnmatch(name, alarm_pattern), name
    bucket_pattern = fn_sub_text(stmts['Buckets']['Resource']).split(':::')[1]
    for logical in ('ObjectsBucket', 'LogsBucket'):
        # CloudFormation's generated name: <stack>-<logical id>-<random>, lowercased.
        assert fnmatch.fnmatch(f'agenticml-hosted-{logical.lower()}-abc123def456', bucket_pattern)
    # Everything the stack creates stays in services the CloudFormation role covers.
    role_services = {a.split(':')[0] for s in stmts.values() if s['Effect'] == 'Allow' for a in actions_of(s)}
    needed = {'AWS::EC2': 'ec2', 'AWS::ElasticLoadBalancingV2': 'elasticloadbalancing', 'AWS::ECS': 'ecs',
              'AWS::RDS': 'rds', 'AWS::EFS': 'elasticfilesystem', 'AWS::S3': 's3', 'AWS::Logs': 'logs',
              'AWS::CloudWatch': 'cloudwatch', 'AWS::Backup': 'backup', 'AWS::IAM': 'iam'}
    for resource in hosted['Resources'].values():
        assert needed[resource['Type'].rsplit('::', 1)[0]] in role_services, resource['Type']


def test_hosted_alarms_notify_the_imported_topic(hosted):
    topic = {'Fn::ImportValue': 'agenticml-hosted-bootstrap-AlertsTopicArn'}
    alarms = alarms_of(hosted)
    assert set(alarms) == {'agenticml-hosted-alb_5xx', 'agenticml-hosted-unhealthy', 'agenticml-hosted-rds_storage',
                           'agenticml-hosted-rds_cpu', 'agenticml-hosted-healthy_hosts',
                           'agenticml-hosted-latency-p95'}
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
        'healthy_hosts': ('AWS/ApplicationELB', 'HealthyHostCount', 'Minimum', None, 'LessThanThreshold', 1, 60, 5, 'breaching'),
        'latency-p95': ('AWS/ApplicationELB', 'TargetResponseTime', None, 'p95', 'GreaterThanThreshold', 5, 300, 1, 'notBreaching'),
    }
    # healthy_hosts (no healthy target for 5 minutes) only exists while
    # DesiredCount is not 0, so scaling to 0 does not leave it in ALARM.
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


def test_deploy_workflow_runs_on_dev_pushes_and_on_dispatch():
    workflow = load_workflow('deploy.yml')
    # PyYAML reads the bare `on` key as True.
    triggers = workflow[True]
    assert set(triggers) == {'push', 'workflow_dispatch'}
    assert triggers['push']['branches'] == ['dev']
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
