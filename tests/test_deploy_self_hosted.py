"""Static checks of deploy/aws-self-hosted/agenticml.yaml (package 36.2).

Nothing here talks to AWS. The template is parsed with the loader of
test_deploy_cloudformation.py; the UserData script is rendered with example
values and checked with ``bash -n``, ``shellcheck`` (when installed) and, for
the parts that decide whether a disk is formatted or what the server is
told, run in a sandbox with stand-in commands.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
import yaml

from test_deploy_cloudformation import (
    INLINE_LAMBDA_LIMIT, TEMPLATE_SIZE_LIMIT, CfnLoader, as_list, only, resources_of, walk)

from agenticml.server import oidc
from agenticml.server.limits import Plans

ROOT = Path(__file__).parents[1]
KIT = ROOT / 'deploy/aws-self-hosted'
TEMPLATE = KIT / 'agenticml.yaml'
README = KIT / 'README.md'
SYSTEMD_UNIT = ROOT / 'deploy/systemd/agenticml-server.service'
WORKFLOW = ROOT / '.github/workflows/test.yml'
ENV_FILE = '/etc/agenticml-server.env'
LIMITS_FILE = '/etc/agenticml-limits.json'
CLI = ROOT / 'src/agenticml/cli.py'

PARAMETERS = {
    'DomainName', 'HostedZoneId', 'CertificateArn', 'AdminEmail', 'SignUp',
    'AllowedEmailDomains', 'CognitoDomainPrefix', 'AgenticmlVersion', 'InstanceType',
    'DataVolumeSize', 'BackupRetentionDays', 'WorkspaceStorageGB', 'PeoplePerWorkspace',
    'Scheme', 'AllowedCidr', 'VpcId', 'PublicSubnetIds', 'AssignPublicIp', 'DataSnapshotId',
}
EXAMPLE = {
    'AWS::Region': 'eu-west-1',
    'AWS::StackName': 'agenticml',
    'AWS::AccountId': '123456789012',
    'AWS::Partition': 'aws',
    'DomainName': 'aml.example.com',
    'AdminEmail': 'admin@example.com',
    'SignUp': 'open',
    'AllowedEmailDomains': 'example.com,example.org',
    'AgenticmlVersion': '3.4.0',
    'WorkspaceStorageGB': '100',
    'PeoplePerWorkspace': '50',
    'DataVolume': 'vol-0123456789abcdef0',
    'UserPool': 'eu-west-1_AbCdEfGhI',
    'UserPoolClient': '1h57kf5cpq17m0eml12EXAMPLE',
    'UserPoolDomain': 'aml-0a1b2c3d',
    'TrustedProxies': '10.42.0.0/24,10.42.1.0/24',
    'DataSnapshotId': '',
}


@pytest.fixture(scope='module')
def text():
    return TEMPLATE.read_text()


@pytest.fixture(scope='module')
def template(text):
    return yaml.load(text, Loader=CfnLoader)


@pytest.fixture(scope='module')
def launch_data(template):
    return only(template, 'AWS::EC2::LaunchTemplate')['Properties']['LaunchTemplateData']


def user_data_source(launch_data):
    """The Fn::Sub source of the UserData, and the Sub variables."""
    sub = launch_data['UserData']['Fn::Base64']['Fn::Sub']
    source, variables = sub
    return source, variables


def render(source, values=None):
    values = {**EXAMPLE, **(values or {})}

    def replace(match):
        name = match.group(1)
        if name.startswith('!'):
            return '${' + name[1:] + '}'
        return values[name]

    return re.sub(r'\$\{([^}]+)\}', replace, source)


@pytest.fixture(scope='module')
def script(launch_data):
    source, _ = user_data_source(launch_data)
    return render(source)


def section(script, start, end):
    """Lines of script from the one starting with start up to, not including, the one starting with end."""
    lines = script.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i, line in enumerate(lines) if i > first and line.startswith(end))
    return '\n'.join(lines[first:last]) + '\n'


# ------------------------------------------------------------------ template

def test_template_default_version_is_the_package_version(template):
    """Protects a stack launched with defaults from installing a version other
    than the release the template was published with."""
    import agenticml
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M).group(1)
    default = template['Parameters']['AgenticmlVersion']['Default']
    assert default == agenticml.__version__ == pyproject


def test_release_workflow_attaches_the_template_to_the_release():
    """Protects the Deploy on AWS page ("download agenticml.yaml from the
    GitHub release"): the workflow that runs when a release is published must
    upload exactly this template, with write access on that job only."""
    path = ROOT / '.github/workflows/release-assets.yml'
    workflow = yaml.safe_load(path.read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert triggers == {'release': {'types': ['published']}}
    assert workflow['permissions'] == {'contents': 'read'}
    (job,) = workflow['jobs'].values()
    assert job['permissions'] == {'contents': 'write'}
    for step in job['steps']:
        if 'uses' in step:
            assert re.search(r'@[0-9a-f]{40}$', step['uses']), step['uses']
    uploads = [s['run'] for s in job['steps'] if 'gh release upload' in s.get('run', '')]
    assert len(uploads) == 1
    assert 'deploy/aws-self-hosted/agenticml.yaml' in uploads[0]
    assert (ROOT / 'deploy/aws-self-hosted/agenticml.yaml').is_file()
    # The release is named by the event, passed through env, never pasted into the script.
    assert '${{' not in uploads[0]
    assert {'GH_TOKEN', 'TAG'} <= {k for s in job['steps'] if 'gh release upload' in s.get('run', '') for k in s['env']}


def test_template_fits_without_a_bucket(text):
    assert len(text.encode()) < TEMPLATE_SIZE_LIMIT


def test_parameters_and_defaults(template):
    params = template['Parameters']
    assert set(params) == PARAMETERS
    required = {name for name, p in params.items() if 'Default' not in p}
    assert required == {'DomainName', 'AdminEmail'}
    defaults = {name: p['Default'] for name, p in params.items() if 'Default' in p}
    assert defaults == {
        'HostedZoneId': '', 'CertificateArn': '', 'SignUp': 'invite-only',
        'AllowedEmailDomains': '', 'CognitoDomainPrefix': '', 'AgenticmlVersion': '3.4.0',
        'InstanceType': 't4g.small', 'DataVolumeSize': '50', 'BackupRetentionDays': '14',
        'WorkspaceStorageGB': '20', 'PeoplePerWorkspace': '50', 'DataSnapshotId': '',
        'Scheme': 'internet-facing', 'AllowedCidr': '0.0.0.0/0', 'VpcId': '',
        'PublicSubnetIds': '', 'AssignPublicIp': 'true'}
    assert params['SignUp']['AllowedValues'] == ['invite-only', 'open']
    assert params['Scheme']['AllowedValues'] == ['internet-facing', 'internal']
    assert all(t.startswith(('t4g.', 'm7g.', 'c7g.')) for t in params['InstanceType']['AllowedValues'])
    assert re.fullmatch(params['AgenticmlVersion']['AllowedPattern'], '3.4.0')
    assert not re.fullmatch(params['AgenticmlVersion']['AllowedPattern'], "3.4.0'; reboot")
    # Numbers are strings with a pattern: a decimal would break the limits file.
    for name in ('DataVolumeSize', 'BackupRetentionDays', 'WorkspaceStorageGB', 'PeoplePerWorkspace'):
        assert params[name]['Type'] == 'String' and 'MaxLength' in params[name]
    # Every default passes its own pattern.
    for name, param in params.items():
        if 'AllowedPattern' in param and 'Default' in param:
            assert re.fullmatch(param['AllowedPattern'], param['Default']), name


def accepts(template, name, value):
    return re.fullmatch(template['Parameters'][name]['AllowedPattern'], value) is not None


@pytest.mark.parametrize('name,good,bad', [
    ('DataVolumeSize', ['10', '50', '16000'], ['', '9', '5', '0', '010', '1.5', '-1', '1e3', '100000', 'ten']),
    ('BackupRetentionDays', ['1', '14', '365', '999'], ['', '0', '01', '1.5', '1000', 'x']),
    ('WorkspaceStorageGB', ['1', '20', '999999'], ['', '0', '2.5', '1000000', 'abc', '5 ']),
    ('PeoplePerWorkspace', ['1', '50', '99999'], ['', '0', '1.0', '100000', '-3']),
    ('AllowedCidr', ['0.0.0.0/0', '10.0.0.0/8', '192.168.1.0/24', '255.255.255.255/32', '203.0.113.7/32'],
     ['', '256.0.0.0/8', '1.2.3.4/33', '1.2.3/24', '1.2.3.4', '01.2.3.4/8', '1.2.3.4/', '1.2.3.4/-1']),
    ('DataSnapshotId', ['', 'snap-0123456789abcdef0'], ['snap-', 'vol-0123456789abcdef0', 'SNAP-0123456789abcdef0']),
    ('PublicSubnetIds', ['', 'subnet-0123abcd,subnet-4567ef01'],
     ['subnet-0123abcd', 'subnet-0123abcd, subnet-4567ef01', 'subnet-0123abcd,']),
])
def test_parameter_patterns(template, name, good, bad):
    for value in good:
        assert accepts(template, name, value), (name, value)
    for value in bad:
        assert not accepts(template, name, value), (name, value)


def _long(length):
    """A valid domain of exactly this many characters (labels of 63, 63, 63, then the rest)."""
    labels, left = [], length
    while left > 0:
        size = min(63, left - 1) if left > 64 else left
        labels.append('a' * size)
        left -= size + 1
    return '.'.join(labels)


DOMAIN_SAMPLES = [
    'example.com', 'a.example.com,b.org', 'EXAMPLE.COM', 'a-b.example.com', 'a1.b2.c3.d4',
    '@example.com', '1.2.3.4', 'a' * 63 + '.com', 'a' * 64 + '.com', ('a' * 63 + '.') * 3 + 'com',
    'a' * 250 + '.com', _long(253), _long(254), _long(259), '-a.example.com', 'a-.example.com', 'example', 'example..com', '.example.com',
    'example.com.', 'exa_mple.com', 'a.b,c', 'a.b,-c.d', 'example.com,evil', 'ex@mple.com', 'a/b.com', '']


def test_allowed_email_domains_pattern_matches_the_servers_parser(template):
    from agenticml.server.webauth import parse_signup_email_domains

    def server_accepts(value):
        try:
            parse_signup_email_domains(value)
        except ValueError:
            return False
        return True

    for value in DOMAIN_SAMPLES:
        assert accepts(template, 'AllowedEmailDomains', value) == server_accepts(value), value
    # The 253-character limit, at the edge on both sides.
    assert len(_long(253)) == 253 and len(_long(254)) == 254 and len(_long(259)) == 259
    assert accepts(template, 'AllowedEmailDomains', _long(253)) and server_accepts(_long(253))
    for length in (254, 259):
        assert not accepts(template, 'AllowedEmailDomains', _long(length))
        assert not server_accepts(_long(length))
    # The pattern is stricter only on whitespace and empty entries, which the server forgives.
    for value in ('a.com, b.com', ' a.com', 'a.com,', ',a.com', 'a.com,,b.org'):
        assert server_accepts(value) and not accepts(template, 'AllowedEmailDomains', value), value
    assert template['Parameters']['AllowedEmailDomains']['MaxLength'] >= 1000


def test_every_parameter_is_in_the_form(template):
    interface = template['Metadata']['AWS::CloudFormation::Interface']
    grouped = [name for group in interface['ParameterGroups'] for name in group['Parameters']]
    assert sorted(grouped) == sorted(PARAMETERS)
    assert set(interface['ParameterLabels']) == PARAMETERS


def readme_parameters():
    rows = {}
    for line in README.read_text().splitlines():
        match = re.match(r'^\| `(\w+)` \| (.+?) \| (.+) \|$', line)
        if match:
            rows[match.group(1)] = match.group(2).strip('`')
    return rows


def test_readme_parameter_table_matches_the_template(template):
    rows = readme_parameters()
    assert set(rows) == PARAMETERS
    for name, param in template['Parameters'].items():
        default = param.get('Default')
        expected = 'required' if default is None else 'empty' if default == '' else str(default)
        assert rows[name] == expected, name


def test_readme_names_every_output(template):
    body = README.read_text()
    paragraph = body[body.index('Outputs:'):].split('\n\n')[0]
    assert set(re.findall(r'`(\w+)`', paragraph)) == set(template['Outputs'])


def evaluate(node, values):
    """Evaluate a Rules expression (the subset CloudFormation Rules allow)."""
    (op, arg), = node.items()
    if op == 'Ref':
        return values[arg]
    if op == 'Fn::Equals':
        return evaluate_all(arg, values)[0] == evaluate_all(arg, values)[1]
    if op == 'Fn::Not':
        return not evaluate(arg[0], values)
    if op == 'Fn::And':
        return all(evaluate(a, values) for a in arg)
    if op == 'Fn::Or':
        return any(evaluate(a, values) for a in arg)
    raise AssertionError(op)


def evaluate_all(args, values):
    return [a if not isinstance(a, dict) else evaluate(a, values) for a in args]


def rule_passes(rule, values):
    condition = rule.get('RuleCondition')
    if condition is not None and not evaluate(condition, values):
        return True
    return all(evaluate(a['Assert'], values) for a in rule['Assertions'])


def test_domain_needs_exactly_one_of_zone_and_certificate(template):
    rule = template['Rules']['DomainOrCertificate']
    base = {'HostedZoneId': '', 'CertificateArn': ''}
    assert not rule_passes(rule, base)
    assert rule_passes(rule, {**base, 'HostedZoneId': 'Z0123456789ABC'})
    assert rule_passes(rule, {**base, 'CertificateArn': 'arn:aws:acm:eu-west-1:123456789012:certificate/x'})
    assert not rule_passes(rule, {'HostedZoneId': 'Z0123456789ABC',
                                  'CertificateArn': 'arn:aws:acm:eu-west-1:123456789012:certificate/x'})


def test_existing_vpc_needs_subnets_and_a_new_one_takes_none(template):
    assert 'VpcCidr' not in template['Parameters']
    needs = template['Rules']['ExistingVpcNeedsSubnets']
    assert not rule_passes(needs, {'VpcId': 'vpc-0123456789abcdef0', 'PublicSubnetIds': ''})
    assert rule_passes(needs, {'VpcId': 'vpc-0123456789abcdef0', 'PublicSubnetIds': 'subnet-a,subnet-b'})
    assert rule_passes(needs, {'VpcId': '', 'PublicSubnetIds': ''})
    new = template['Rules']['NewVpcTakesNoSubnets']
    new_vpc = {'VpcId': '', 'PublicSubnetIds': '', 'AssignPublicIp': 'true'}
    assert rule_passes(new, new_vpc)
    assert not rule_passes(new, {**new_vpc, 'PublicSubnetIds': 'subnet-a,subnet-b'})
    # The stack's own VPC has no NAT: a private instance could not reach PyPI.
    assert not rule_passes(new, {**new_vpc, 'AssignPublicIp': 'false'})
    existing = {'VpcId': 'vpc-0123456789abcdef0', 'PublicSubnetIds': 'subnet-a,subnet-b'}
    assert rule_passes(new, {**existing, 'AssignPublicIp': 'false'})


def test_vpc_is_created_only_without_vpc_id(template):
    assert template['Conditions']['CreateVpc'] == {'Fn::Equals': [{'Ref': 'VpcId'}, '']}
    for type_ in ('AWS::EC2::VPC', 'AWS::EC2::InternetGateway', 'AWS::EC2::Subnet',
                  'AWS::EC2::Route', 'AWS::EC2::RouteTable'):
        for name, resource in resources_of(template, type_).items():
            assert resource['Condition'] == 'CreateVpc', name
    # The existing-VPC path gets its zone from the custom resource.
    volume = only(template, 'AWS::EC2::Volume')['Properties']['AvailabilityZone']
    assert volume == {'Fn::If': ['CreateVpc', {'Fn::GetAtt': 'SubnetA.AvailabilityZone'},
                                 {'Fn::GetAtt': 'SubnetZone.AvailabilityZone'}]}
    assert template['Resources']['SubnetZone']['Condition'] == 'ExistingVpc'


def test_balancer_and_instance_wait_for_the_routes(template):
    # DependsOn cannot name conditional resources, so a Ref inside Fn::If orders them.
    for name in ('SubnetARoutes', 'SubnetBRoutes'):
        assert template['Resources'][name]['DependsOn'] == 'DefaultRoute'
    for resource in ('LoadBalancer', 'AutoScalingGroup'):
        tags = template['Resources'][resource]['Properties']['Tags']
        (tag,) = [t for t in tags if t['Key'] == 'agenticml:network']
        assert tag['Value']['Fn::If'][0] == 'CreateVpc'
        assert 'SubnetARoutes' in str(tag['Value'])


def test_launch_template_is_hardened(launch_data):
    assert launch_data['MetadataOptions'] == {
        'HttpEndpoint': 'enabled', 'HttpTokens': 'required', 'HttpPutResponseHopLimit': 1}
    assert 'KeyName' not in launch_data
    (root,) = launch_data['BlockDeviceMappings']
    assert root['Ebs']['Encrypted'] is True
    assert 'arm64' in launch_data['ImageId']
    (nic,) = launch_data['NetworkInterfaces']
    assert nic['Groups'] == [{'Ref': 'InstanceSecurityGroup'}]
    assert nic['AssociatePublicIpAddress'] == {'Fn::If': ['PublicIp', True, False]}


def test_data_volume_is_encrypted_and_survives_the_stack(template):
    volume = only(template, 'AWS::EC2::Volume')
    assert volume['Properties']['Encrypted'] is True
    assert volume['Properties']['VolumeType'] == 'gp3'
    assert volume['DeletionPolicy'] == 'Snapshot'
    assert volume['UpdateReplacePolicy'] == 'Snapshot'
    tags = {t['Key'] for t in volume['Properties']['Tags']}
    assert 'agenticml:data' in tags
    policy = only(template, 'AWS::DLM::LifecyclePolicy')['Properties']['PolicyDetails']
    assert policy['ResourceTypes'] == ['VOLUME']
    assert {t['Key'] for t in policy['TargetTags']} == {'agenticml:data'}
    (schedule,) = policy['Schedules']
    assert schedule['RetainRule'] == {'Count': {'Ref': 'BackupRetentionDays'}}


def test_one_instance_and_never_two_writers(template):
    group = only(template, 'AWS::AutoScaling::AutoScalingGroup')
    props = group['Properties']
    assert props['MinSize'] == props['MaxSize'] == props['DesiredCapacity'] == '1'
    update = group['UpdatePolicy']['AutoScalingRollingUpdate']
    assert update['MinInstancesInService'] == 0
    assert update['MaxBatchSize'] == 1
    assert update['WaitOnResourceSignals'] is True
    assert group['CreationPolicy']['ResourceSignal']['Count'] == 1
    assert props['HealthCheckType'] == 'ELB'
    assert len(props['VPCZoneIdentifier']) == 1


def test_no_ssh_and_instance_reachable_only_from_the_load_balancer(template):
    groups = resources_of(template, 'AWS::EC2::SecurityGroup')
    assert set(groups) == {'AlbSecurityGroup', 'InstanceSecurityGroup'}
    assert not resources_of(template, 'AWS::EC2::SecurityGroupIngress')
    for name, group in groups.items():
        for rule in group['Properties']['SecurityGroupIngress']:
            assert rule['FromPort'] <= rule['ToPort']
            assert not rule['FromPort'] <= 22 <= rule['ToPort'], name
            assert rule['IpProtocol'] == 'tcp'
    (rule,) = groups['InstanceSecurityGroup']['Properties']['SecurityGroupIngress']
    assert (rule['FromPort'], rule['ToPort']) == (8080, 8080)
    assert rule['SourceSecurityGroupId'] == {'Ref': 'AlbSecurityGroup'}
    assert 'CidrIp' not in rule
    assert 'SecurityGroupEgress' not in groups['InstanceSecurityGroup']['Properties']
    alb = groups['AlbSecurityGroup']['Properties']['SecurityGroupIngress']
    assert {r['FromPort'] for r in alb} == {80, 443}
    assert all(r['CidrIp'] == {'Ref': 'AllowedCidr'} for r in alb)


def test_load_balancer(template):
    alb = only(template, 'AWS::ElasticLoadBalancingV2::LoadBalancer')['Properties']
    attributes = {a['Key']: a['Value'] for a in alb['LoadBalancerAttributes']}
    assert attributes['routing.http.drop_invalid_header_fields.enabled'] == 'true'
    assert attributes['access_logs.s3.enabled'] == 'false'
    assert alb['Scheme'] == {'Ref': 'Scheme'}
    target = only(template, 'AWS::ElasticLoadBalancingV2::TargetGroup')['Properties']
    assert target['Port'] == 8080 and target['HealthCheckPath'] == '/healthz'
    attributes = {a['Key']: a['Value'] for a in target['TargetGroupAttributes']}
    assert attributes['deregistration_delay.timeout_seconds'] == '30'
    listeners = {l['Properties']['Port']: l['Properties']
                 for l in resources_of(template, 'AWS::ElasticLoadBalancingV2::Listener').values()}
    assert listeners[443]['SslPolicy'] == 'ELBSecurityPolicy-TLS13-1-2-2021-06'
    (action,) = listeners[80]['DefaultActions']
    assert action['Type'] == 'redirect'
    assert action['RedirectConfig'] == {'Protocol': 'HTTPS', 'Port': '443', 'StatusCode': 'HTTP_301'}


def test_certificate_and_record_only_with_a_hosted_zone(template):
    assert template['Conditions']['CreateCertificate'] == {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'HostedZoneId'}, '']}]}
    cert = only(template, 'AWS::CertificateManager::Certificate')
    assert cert['Condition'] == 'CreateCertificate'
    assert cert['Properties']['ValidationMethod'] == 'DNS'
    assert only(template, 'AWS::Route53::RecordSet')['Condition'] == 'CreateCertificate'
    listener = template['Resources']['HttpsListener']['Properties']['Certificates']
    assert listener == [{'CertificateArn': {'Fn::If': [
        'CreateCertificate', {'Ref': 'Certificate'}, {'Ref': 'CertificateArn'}]}}]


def test_cognito_client_matches_the_server(template):
    client = only(template, 'AWS::Cognito::UserPoolClient')['Properties']
    assert client['GenerateSecret'] is False
    assert client['AllowedOAuthFlows'] == ['code']
    assert client['AllowedOAuthFlowsUserPoolClient'] is True
    assert client['AllowedOAuthScopes'] == ['openid', 'email', 'profile']
    assert client['PreventUserExistenceErrors'] == 'ENABLED'
    assert client['CallbackURLs'] == [{'Fn::Sub': 'https://${DomainName}' + oidc.CALLBACK_PATH}]
    assert client['LogoutURLs'] == [{'Fn::Sub': 'https://${DomainName}/login'}]
    pool = only(template, 'AWS::Cognito::UserPool')
    props = pool['Properties']
    assert props['UserPoolTier'] == 'LITE'
    assert props['DeletionProtection'] == 'ACTIVE'
    assert props['UsernameAttributes'] == ['email'] and props['AutoVerifiedAttributes'] == ['email']
    assert props['Policies']['PasswordPolicy']['MinimumLength'] >= 12
    assert pool['DeletionPolicy'] == 'Retain'
    assert 'ManagedLoginBranding' not in str(template['Resources'])


def test_pre_sign_up_filter_exists_only_with_domains(template):
    assert template['Conditions']['RestrictDomains'] == {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'AllowedEmailDomains'}, '']}]}
    for name in ('PreSignUpFunction', 'PreSignUpRole', 'PreSignUpPermission'):
        assert template['Resources'][name]['Condition'] == 'RestrictDomains'
    config = template['Resources']['UserPool']['Properties']['LambdaConfig']
    assert config['Fn::If'][0] == 'RestrictDomains'
    assert config['Fn::If'][2] == {'Ref': 'AWS::NoValue'}


def granted_actions(template):
    role = template['Resources']['InstanceRole']['Properties']
    granted = set()
    for policy in role['Policies']:
        for statement in as_list(policy['PolicyDocument']['Statement']):
            assert statement['Effect'] == 'Allow'
            granted |= set(as_list(statement['Action']))
    return granted


def test_instance_role_is_narrow(template):
    role = template['Resources']['InstanceRole']['Properties']
    assert role['ManagedPolicyArns'] == [
        {'Fn::Sub': 'arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore'}]
    assert granted_actions(template) == {
        'ec2:AttachVolume', 'ec2:DescribeVolumes', 'cloudformation:SignalResource'}
    attach = []
    for policy in role['Policies']:
        for statement in as_list(policy['PolicyDocument']['Statement']):
            if 'ec2:AttachVolume' in as_list(statement['Action']):
                attach.append(statement)
            if statement['Resource'] == '*':
                assert statement['Action'] == 'ec2:DescribeVolumes'
    assert len(attach) == 2
    assert all(s['Resource'] != '*' for s in attach)
    assert any('${DataVolume}' in s['Resource']['Fn::Sub'] for s in attach)
    assert any('Condition' in s for s in attach)


def test_every_aws_call_in_user_data_is_allowed(template, script):
    needed = set()
    for service, call in re.findall(r'\baws (ec2|s3|ssm|cloudformation) ([a-z-]+)', script):
        needed.add(f"{service}:{''.join(part.title() for part in call.split('-'))}")
    assert needed == {'ec2:DescribeVolumes', 'ec2:AttachVolume'}
    assert '/opt/aws/bin/cfn-signal' in script
    needed.add('cloudformation:SignalResource')
    assert needed <= granted_actions(template)
    assert 'aws ' not in re.sub(r'aws ec2 (describe-volumes|attach-volume)|aws-cfn-bootstrap', '', script)


def commands(script):
    """Script lines that are commands: no comments, no echo."""
    return [line.strip() for line in script.splitlines()
            if line.strip() and not line.strip().startswith(('#', 'echo '))]


def test_script_shape(script):
    assert script.startswith('#!/bin/bash\n')
    assert commands(script)[0] == 'set -Eeuo pipefail'
    trap = next(line for line in commands(script) if line.startswith('trap ') and line.endswith('EXIT'))
    assert '--success false' in trap
    healthz = script.index('/healthz')
    assert script.index('systemctl enable --now agenticml-server') < healthz
    assert healthz < script.index('signal --success true') < script.index('trap - EXIT')
    assert script.count('signal --success true') == 1


def test_stack_creation_waits_for_the_server(template, launch_data):
    source, _ = user_data_source(launch_data)
    (group,) = resources_of(template, 'AWS::AutoScaling::AutoScalingGroup')
    assert f'--resource {group}' in source
    assert template['Resources'][group]['CreationPolicy']['ResourceSignal']['Count'] == 1


def test_outputs(template):
    assert set(template['Outputs']) == {
        'Url', 'FirstSteps', 'SyncCommand', 'AlbDnsName', 'DataVolumeId', 'CognitoUserPoolId',
        'SessionManagerCommand'}


def test_no_iam_wildcard_actions(template):
    for node in walk(template):
        if isinstance(node, dict) and 'Action' in node and 'Effect' in node:
            for action in as_list(node['Action']):
                assert action != '*' and not action.lower().startswith('iam:')


def test_load_balancer_wiring(template):
    alb = template['Resources']['LoadBalancer']['Properties']
    assert alb['SecurityGroups'] == [{'Ref': 'AlbSecurityGroup'}]
    target = template['Resources']['TargetGroup']['Properties']
    assert target['Matcher'] == {'HttpCode': '200'}
    assert target['HealthCheckPath'] == '/healthz'
    for listener in ('HttpsListener', 'HttpListener'):
        assert template['Resources'][listener]['Properties']['LoadBalancerArn'] == {'Ref': 'LoadBalancer'}
    (action,) = template['Resources']['HttpsListener']['Properties']['DefaultActions']
    assert action == {'Type': 'forward', 'TargetGroupArn': {'Ref': 'TargetGroup'}}
    group = template['Resources']['AutoScalingGroup']
    assert group['Properties']['TargetGroupARNs'] == [{'Ref': 'TargetGroup'}]
    assert group['DependsOn'] == 'HttpsListener'
    alias = template['Resources']['DnsRecord']['Properties']
    assert alias['AliasTarget'] == {
        'DNSName': {'Fn::GetAtt': 'LoadBalancer.DNSName'},
        'HostedZoneId': {'Fn::GetAtt': 'LoadBalancer.CanonicalHostedZoneID'}}
    assert alias['Name'] == {'Ref': 'DomainName'} and alias['Type'] == 'A'
    permission = template['Resources']['PreSignUpPermission']['Properties']
    assert permission['Principal'] == 'cognito-idp.amazonaws.com'
    assert permission['SourceArn'] == {'Fn::GetAtt': 'UserPool.Arn'}
    assert permission['Action'] == 'lambda:InvokeFunction'
    assert permission['FunctionName'] == {'Ref': 'PreSignUpFunction'}


def test_data_volume_can_be_restored_from_a_snapshot(template):
    assert template['Conditions']['FromSnapshot'] == {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'DataSnapshotId'}, '']}]}
    props = only(template, 'AWS::EC2::Volume')['Properties']
    assert props['SnapshotId'] == {'Fn::If': ['FromSnapshot', {'Ref': 'DataSnapshotId'},
                                              {'Ref': 'AWS::NoValue'}]}


def test_cognito_hardening(template):
    pool = template['Resources']['UserPool']['Properties']
    assert pool['UserAttributeUpdateSettings'] == {'AttributesRequireVerificationBeforeUpdate': ['email']}
    client = template['Resources']['UserPoolClient']['Properties']
    # The hosted UI sign-up writes email; changing it later needs verification.
    assert 'email' in client['WriteAttributes'] and 'name' in client['WriteAttributes']
    assert 'ALLOW_USER_SRP_AUTH' in client['ExplicitAuthFlows']


def test_ci_lints_the_template_and_builds_the_image_only_for_the_hosted_files():
    workflow = WORKFLOW.read_text()
    job = workflow[workflow.index('  deploy-check:'):]
    assert 'cfn-lint==1.60.0' in job
    assert 'deploy/aws-self-hosted/agenticml.yaml' in job
    lint = job[job.index('name: Lint CloudFormation templates'):job.index('name: Build and check hosted image')]
    assert "steps.changes.outputs.lint == 'true'" in lint
    image = job[job.index('name: Build and check hosted image'):]
    assert "steps.changes.outputs.run == 'true'" in image and 'docker build' in image
    # The self-hosted branch sets lint only, so the image is not built for it.
    branch = job[job.index("elif grep -Eq '^deploy/aws-self-hosted/'"):].split('fi\n')[0]
    assert "echo 'lint=true'" in branch and "echo 'run=true'" not in branch
    # The hosted files set both, so the image is still built for them.
    first = job[job.index('if [[ "$EVENT_NAME" != pull_request ]]'):job.index("elif grep -Eq '^deploy/aws-self-hosted/'")]
    assert "echo 'run=true'" in first and "echo 'lint=true'" in first


# ------------------------------------------------------------------ UserData

def test_user_data_names_only_things_that_exist(template, launch_data):
    source, variables = user_data_source(launch_data)
    known = (set(template['Parameters']) | set(template['Resources']) | set(variables)
             | {'AWS::Region', 'AWS::StackName', 'AWS::AccountId', 'AWS::Partition'})
    for name in re.findall(r'\$\{([^}!]+)\}', source):
        assert name in known, name
    assert set(variables) == {'TrustedProxies'}
    # The ALB's subnets: the created ones are written out, the existing ones come from the Lambda.
    assert variables['TrustedProxies'] == {'Fn::If': [
        'CreateVpc', '10.42.0.0/24,10.42.1.0/24', {'Fn::GetAtt': 'SubnetZone.CidrBlocks'}]}
    created = [template['Resources'][n]['Properties']['CidrBlock'] for n in ('SubnetA', 'SubnetB')]
    assert ','.join(created) == '10.42.0.0/24,10.42.1.0/24'
    assert only(template, 'AWS::EC2::VPC')['Properties']['CidrBlock'] == '10.42.0.0/16'
    assert template['Resources']['LoadBalancer']['Properties']['Subnets']['Fn::If'][1] == [
        {'Ref': 'SubnetA'}, {'Ref': 'SubnetB'}]


def test_user_data_env_names_are_server_options(script):
    # Real names: the ones cli.py reads, and the ones SERVER.md's option table lists.
    in_cli = set(re.findall(r'AGENTICML_SERVER_[A-Z0-9_]+', CLI.read_text()))
    table = (ROOT / 'docs/SERVER.md').read_text()
    in_docs = set(re.findall(r'^\| `--[^|]*` \| `(AGENTICML_SERVER_[A-Z0-9_]+)` \|', table, re.M))
    assert 'AGENTICML_SERVER_OIDC_ISSUER' in in_cli
    written = set(re.findall(r'AGENTICML_SERVER_[A-Z0-9_]+', script))
    assert written <= in_cli, written - in_cli
    # The options that predate package 36 are in SERVER.md's table (36.3 adds the new ones).
    assert {'AGENTICML_SERVER_PUBLIC_URL', 'AGENTICML_SERVER_TRUSTED_PROXIES',
            'AGENTICML_SERVER_ADMIN_EMAILS', 'AGENTICML_SERVER_LIMITS',
            'AGENTICML_SERVER_OPEN_SIGNUP'} <= in_docs
    assert written == {
        'AGENTICML_SERVER_PUBLIC_URL', 'AGENTICML_SERVER_TRUSTED_PROXIES',
        'AGENTICML_SERVER_OIDC_ISSUER', 'AGENTICML_SERVER_OIDC_CLIENT_ID',
        'AGENTICML_SERVER_OIDC_LOGOUT_URL', 'AGENTICML_SERVER_ADMIN_EMAILS',
        'AGENTICML_SERVER_LIMITS', 'AGENTICML_SERVER_OPEN_SIGNUP',
        'AGENTICML_SERVER_SIGNUP_EMAIL_DOMAINS'}


def test_server_env_file_content(script):
    from agenticml.server.webauth import parse_trusted_proxies

    env = section(script, f"cat > {ENV_FILE}", 'EOF')
    lines = dict(line.split('=', 1) for line in env.splitlines()[1:])
    assert lines['AGENTICML_SERVER_PUBLIC_URL'] == 'https://aml.example.com'
    assert lines['AGENTICML_SERVER_OIDC_ISSUER'] == (
        'https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_AbCdEfGhI')
    assert lines['AGENTICML_SERVER_OIDC_CLIENT_ID'] == '1h57kf5cpq17m0eml12EXAMPLE'
    assert lines['AGENTICML_SERVER_OIDC_LOGOUT_URL'] == (
        'https://aml-0a1b2c3d.auth.eu-west-1.amazoncognito.com/logout')
    assert lines['AGENTICML_SERVER_LIMITS'] == LIMITS_FILE
    assert lines['AGENTICML_SERVER_ADMIN_EMAILS'] == 'admin@example.com'
    # The load balancer's subnets, in the server's own format; never "everything".
    proxies = parse_trusted_proxies(lines['AGENTICML_SERVER_TRUSTED_PROXIES'].split(','))
    assert [str(net) for net in proxies] == ['10.42.0.0/24', '10.42.1.0/24']
    # No secret of any kind.
    assert not re.search(r'KEY|SECRET|PASSWORD|TOKEN', env)


def run_block(block, tmp_path, stubs=None, env=None):
    """Run a script block with stand-in commands; return (exit code, lines the stubs logged, output)."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / 'calls'
    calls.touch()
    for name, body in (stubs or {}).items():
        stub = bin_dir / name
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> {calls}\n{body}\n')
        stub.chmod(0o755)
    run = subprocess.run(
        ['bash', '-Eeuo', 'pipefail', '-c', block],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', **(env or {})},
        capture_output=True, text=True)
    return run.returncode, calls.read_text().splitlines(), run.stdout + run.stderr


def fresh(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return folder


def run_env_block(source, tmp_path, values):
    script = render(source, values)
    block = section(script, f"cat > {ENV_FILE}", 'chown root:root')
    path = tmp_path / 'server.env'
    code, _, output = run_block(block.replace(ENV_FILE, str(path)), tmp_path)
    assert code == 0, output
    return path.read_text()


def test_open_signup_and_domains_are_written_only_when_chosen(launch_data, tmp_path):
    source, _ = user_data_source(launch_data)
    off = run_env_block(source, fresh(tmp_path, 'off'), {'SignUp': 'invite-only', 'AllowedEmailDomains': ''})
    assert 'OPEN_SIGNUP' not in off and 'SIGNUP_EMAIL_DOMAINS' not in off
    on = run_env_block(source, fresh(tmp_path, 'on'),
                       {'SignUp': 'open', 'AllowedEmailDomains': 'example.com,example.org'})
    assert 'AGENTICML_SERVER_OPEN_SIGNUP=1\n' in on
    assert 'AGENTICML_SERVER_SIGNUP_EMAIL_DOMAINS=example.com,example.org\n' in on


def test_limits_file_loads_with_the_servers_loader(launch_data, tmp_path):
    source, _ = user_data_source(launch_data)
    script = render(source, {'WorkspaceStorageGB': '250', 'PeoplePerWorkspace': '12'})
    block = section(script, 'STORAGE_BYTES=', f'cat > {ENV_FILE}')
    path = tmp_path / 'limits.json'
    code, _, output = run_block(block.replace(LIMITS_FILE, str(path)), tmp_path)
    assert code == 0, output
    json.loads(path.read_text())
    plans = Plans.load(path)
    assert plans.free['storage_bytes'] == 250 * 10 ** 9
    assert plans.free['upload_bytes_per_day'] == 250 * 10 ** 9
    assert plans.free['people'] == 12
    assert plans.free_workspaces_per_account == 1


def test_systemd_unit_keeps_the_kits_hardening(script):
    unit = section(script, 'cat > /etc/systemd/system/agenticml-server.service', 'EOF')
    ours = [line for line in unit.splitlines()[1:] if line]
    kit = [line for line in SYSTEMD_UNIT.read_text().splitlines()
           if line and not line.startswith(('#', 'Environment=', 'ExecStart=', 'EnvironmentFile='))]
    for line in kit:
        assert line in ours, line
    assert f'EnvironmentFile={ENV_FILE}' in ours
    assert 'RequiresMountsFor=/var/lib/agenticml-server' in ours
    # Grown as root ("+" ignores User= and the sandboxing options), before the server starts.
    assert [line for line in ours if line.startswith('ExecStartPre=')] == [
        'ExecStartPre=+/usr/sbin/xfs_growfs /var/lib/agenticml-server']
    (start,) = [line for line in ours if line.startswith('ExecStart=')]
    words = start.split()
    assert words[0] == 'ExecStart=/opt/agenticml/bin/agenticml'
    assert words[1:3] == ['server', 'serve']
    flags = dict(zip(words[3::2], words[4::2]))
    assert flags == {'--data': '/var/lib/agenticml-server', '--host': '0.0.0.0', '--port': '8080'}
    for flag in flags:
        assert f'"{flag}"' in CLI.read_text()


def test_installs_from_pypi_with_python_311(script):
    assert 'python3.11 -m venv /opt/agenticml' in script
    assert "pip install --no-cache-dir 'agenticml-lite[oidc]==3.4.0'" in script
    assert 'dnf install -y python3.11 python3.11-pip aws-cfn-bootstrap xfsprogs' in script


def test_user_data_is_valid_bash(script, tmp_path):
    path = tmp_path / 'userdata.sh'
    path.write_text(script)
    run = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    shellcheck = shutil.which('shellcheck')
    if shellcheck is None:
        pytest.skip('shellcheck is not installed (bash -n passed)')
    run = subprocess.run([shellcheck, '-S', 'warning', str(path)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout


def test_user_data_is_not_a_secret_store(script):
    assert not re.search(r'(?i)secret|password|api[_-]?key|BEGIN [A-Z ]*PRIVATE', script)


# ------------------------------------------------------------ the data volume

def format_block(script, restored=''):
    block = section(script, 'RESTORED_FROM=', 'DATA_UUID=')
    assert "RESTORED_FROM=''" in block
    return block.replace("RESTORED_FROM=''", f"RESTORED_FROM='{restored}'")


# blkid -p as it really behaves: a file system names its type, a partitioned or
# unrecognised device exits 0 with nothing printed, a blank device exits 2.
BLKID = {
    'filesystem': 'echo xfs',
    'partitioned': 'exit 0',
    'blank': 'exit 2',
    'error1': 'exit 1',
    'error4': 'exit 4',
    'error8': 'exit 8',
}


def run_format(script, tmp_path, device, restored=''):
    return run_block(format_block(script, restored), tmp_path,
                     {'blkid': BLKID[device], 'mkfs.xfs': 'true'}, env={'DEVICE': '/dev/stub'})


def formatted(calls):
    return [call for call in calls if call.startswith('mkfs.xfs')]


def test_a_device_with_a_file_system_is_never_formatted(script, tmp_path):
    code, calls, _ = run_format(script, tmp_path, 'filesystem')
    assert code == 0 and not formatted(calls)


def test_a_blank_device_is_formatted(script, tmp_path):
    code, calls, _ = run_format(script, tmp_path, 'blank')
    assert code == 0 and formatted(calls) == ['mkfs.xfs -L aml-data /dev/stub']


def test_a_partitioned_device_is_never_formatted(script, tmp_path):
    code, calls, output = run_format(script, tmp_path, 'partitioned')
    assert code == 1 and not formatted(calls)
    assert 'partition table' in output


@pytest.mark.parametrize('device', ['error1', 'error4', 'error8'])
def test_a_blkid_error_never_formats(script, tmp_path, device):
    code, calls, _ = run_format(script, tmp_path, device)
    assert code == 1 and not formatted(calls)


def test_a_blank_volume_is_refused_when_it_should_hold_a_snapshot(script, tmp_path):
    snapshot = 'snap-0123456789abcdef0'
    code, calls, output = run_format(script, fresh(tmp_path, 'blank'), 'blank', restored=snapshot)
    assert code == 1 and not formatted(calls) and snapshot in output
    # A restored volume that has its file system goes on as usual.
    code, calls, _ = run_format(script, fresh(tmp_path, 'fs'), 'filesystem', restored=snapshot)
    assert code == 0 and not formatted(calls)


def test_the_snapshot_id_reaches_the_script(launch_data):
    source, _ = user_data_source(launch_data)
    assert "RESTORED_FROM='snap-0123456789abcdef0'" in render(source, {'DataSnapshotId': 'snap-0123456789abcdef0'})


def test_only_the_resolved_device_reaches_blkid_and_mkfs(script):
    assert 'DEVICE=$(readlink -f "$LINK")' in script
    for line in commands(script):
        if re.search(r'\b(blkid|mkfs\.xfs)\b', line) and not line.startswith(('if ', 'elif ')):
            assert '"$DEVICE"' in line, line
    assert script.count('mkfs.xfs') == script.count('mkfs.xfs -L aml-data "$DEVICE"') == 1
    assert 'nvme-Amazon_Elastic_Block_Store_$VOLUME_NODASH' in script
    assert script.index("tr -d '-'") < script.index('/dev/disk/by-id') < script.index('DEVICE=$(')
    assert script.index('blkid') < script.index('mkfs.xfs') < script.index('/etc/fstab')
    assert 'UUID=$DATA_UUID /var/lib/agenticml-server xfs defaults,nofail 0 2' in script


def attach_block(script):
    return section(script, 'VOLUME_ID=', 'VOLUME_NODASH=')


# describe-volumes answers from STEPS, one line per loop pass: "state|holder" or FAIL.
AWS_STUB = r"""\
count=$(cat COUNT 2>/dev/null || echo 0)
case "$*" in
  *Volumes\[0\].State*) count=$((count + 1)); echo "$count" > COUNT; field=1 ;;
  *Volumes\[0\].Attachments\[0\].InstanceId*) field=2 ;;
  *attach-volume*) exit 0 ;;
esac
line=$(sed -n "${count}p" STEPS)
[ -n "$line" ] || line=$(tail -n 1 STEPS)
[ "$line" != FAIL ] || exit 1
echo "$line" | cut -d'|' -f"$field"
"""


def run_attach(script, tmp_path, steps):
    (tmp_path / 'STEPS').write_text('\n'.join(steps) + '\n')
    stub = AWS_STUB.replace('COUNT', str(tmp_path / 'COUNT')).replace('STEPS', str(tmp_path / 'STEPS'))
    return run_block(attach_block(script), tmp_path, {'aws': stub, 'sleep': 'true'},
                     env={'INSTANCE_ID': 'i-new', 'AWS_DEFAULT_REGION': 'eu-west-1'})


def test_attach_waits_for_the_old_instance_and_survives_a_failed_describe(script, tmp_path):
    code, calls, output = run_attach(script, tmp_path, [
        'FAIL', 'in-use|i-old', 'in-use|i-old', 'available|None', 'in-use|i-new'])
    assert code == 0, output
    attaches = [c for c in calls if 'attach-volume' in c]
    assert len(attaches) == 1 and '--instance-id i-new' in attaches[0] and '--device /dev/sdf' in attaches[0]


def test_attach_gives_up_after_about_five_minutes(script, tmp_path):
    code, calls, output = run_attach(script, tmp_path, ['in-use|i-old'])
    assert code == 1 and 'did not attach within 5 minutes' in output
    assert len([c for c in calls if 'Volumes[0].State' in c]) == 30     # 30 x 10 s
    assert not any('attach-volume' in c for c in calls)
    assert 'sleep 10' in attach_block(script)


def test_attach_fails_loudly_when_describe_never_works(script, tmp_path):
    code, _, output = run_attach(script, tmp_path, ['FAIL'])
    assert code == 1 and 'did not attach' in output


def ownership_block(script):
    return section(script, 'AML_ID=', 'python3.11 -m venv')


def run_ownership(script, tmp_path, *, user_exists, uid='5400', gid='5400', owner='root'):
    exists = 0 if user_exists else 2
    stubs = {
        'getent': f'case "$1" in group|passwd) exit {exists} ;; esac',
        'groupadd': 'true', 'useradd': 'true', 'chown': 'true', 'chmod': 'true',
        'id': f'case "$1" in -u) echo {uid} ;; -g) echo {gid} ;; esac',
        'stat': f'echo {owner}',
    }
    return run_block(ownership_block(script), tmp_path, stubs)


def test_service_user_has_fixed_ids(script, tmp_path):
    code, calls, output = run_ownership(script, fresh(tmp_path, 'new'), user_exists=False)
    assert code == 0, output
    assert 'groupadd --system --gid 5400 agenticml' in calls
    assert any(c.startswith('useradd --system --uid 5400 --gid 5400 ') for c in calls)
    # An existing user is not created again.
    code, calls, _ = run_ownership(script, fresh(tmp_path, 'old'), user_exists=True)
    assert code == 0 and not any(c.startswith(('useradd', 'groupadd')) for c in calls)


def test_service_user_with_another_id_stops_the_bootstrap(script, tmp_path):
    code, calls, output = run_ownership(script, tmp_path, user_exists=True, uid='993')
    assert code == 1 and 'does not have id 5400' in output


def test_service_user_with_another_group_id_stops_the_bootstrap(script, tmp_path):
    code, calls, output = run_ownership(script, tmp_path, user_exists=True, uid='5400', gid='993')
    assert code == 1 and 'does not have id 5400' in output
    assert not any(c.startswith('chown') for c in calls)


def test_files_of_another_owner_are_taken_over_before_the_server_starts(script, tmp_path):
    code, calls, _ = run_ownership(script, fresh(tmp_path, 'foreign'), user_exists=True, owner='root')
    assert code == 0
    assert 'chown -R agenticml:agenticml /var/lib/agenticml-server' in calls
    code, calls, _ = run_ownership(script, fresh(tmp_path, 'mine'), user_exists=True, owner='agenticml')
    assert code == 0 and not any(c.startswith('chown') for c in calls)
    assert script.index('AML_ID=5400') < script.index('python3.11 -m venv') < script.index(
        'systemctl enable --now agenticml-server')


def seed_block(script, admin=None):
    block = section(script, 'ADMIN_LOGIN=', 'cat > /etc/systemd/system/agenticml-server.service')
    return block if admin is None else block.replace('admin@example.com', admin)


def run_seed(script, tmp_path, data, admin):
    """Run the first-boot seed against a real store, with the real CLI."""
    cli_stub = tmp_path / 'agenticml'
    cli_stub.write_text(f'#!/bin/bash\nexec {sys.executable} -m agenticml "$@"\n')
    cli_stub.chmod(0o755)
    block = seed_block(script, admin).replace('/opt/agenticml/bin/agenticml', str(cli_stub))
    block = block.replace('/var/lib/agenticml-server', str(data))
    return run_block(block, tmp_path, {'runuser': 'shift 3; exec "$@"'})


def cli_json(args):
    run = subprocess.run([sys.executable, '-m', 'agenticml', 'server', *args, '--json'],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_first_boot_makes_the_admin_owner_of_the_default_workspace(script, tmp_path):
    data = fresh(tmp_path, 'data')
    code, _, output = run_seed(script, fresh(tmp_path, 'first'), data, admin='Admin@Example.com')
    assert code == 0, output
    assert [a['login'] for a in cli_json(['accounts', 'list', '--data', str(data)])] == ['admin@example.com']
    users = cli_json(['users', 'list', '--data', str(data)])
    assert [(u['name'], u['role']) for u in users] == [('admin@example.com', 'owner')]
    # No password and no identity: only a verified sign-in with this email claims the account.
    assert users[0]['identity'] is None
    # A second boot (or a replacement instance) changes nothing, even with another admin email.
    code, _, output = run_seed(script, fresh(tmp_path, 'second'), data, admin='someone.else@example.com')
    assert code == 0, output
    assert [a['login'] for a in cli_json(['accounts', 'list', '--data', str(data)])] == ['admin@example.com']
    assert [(u['name'], u['role']) for u in cli_json(['users', 'list', '--data', str(data)])] == [
        ('admin@example.com', 'owner')]


def test_a_failing_cli_stops_the_bootstrap_instead_of_skipping_the_seed(script, tmp_path):
    block = seed_block(script, 'admin@example.com')
    code, calls, _ = run_block(block, tmp_path, {'runuser': 'exit 1'})
    assert code == 1 and len(calls) == 1


def test_the_seed_runs_before_the_service_starts_and_as_the_service_user(script):
    seed = seed_block(script)
    assert 'runuser -u agenticml --' in seed
    assert script.index('ADMIN_LOGIN=') < script.index('systemctl enable --now agenticml-server')
    assert script.index('ADMIN_LOGIN=') > script.index(f'chmod 0600 {ENV_FILE}')
    assert 'aml accounts add "$ADMIN_LOGIN"' in seed and 'aml workspaces set-owner default --owner' in seed


# ------------------------------------------------------------ inline Lambdas

def inline_sources(template):
    return {name: res['Properties']['Code']['ZipFile']
            for name, res in resources_of(template, 'AWS::Lambda::Function').items()}


def test_inline_lambdas_fit_the_zipfile_limit(template):
    sources = inline_sources(template)
    assert set(sources) == {'SubnetZoneFunction', 'PreSignUpFunction'}
    for name, source in sources.items():
        assert len(source.encode()) <= INLINE_LAMBDA_LIMIT, name
        compile(source, name, 'exec')


def load_lambda(template, name, monkeypatch, env=None, ec2=None):
    sent = []
    cfnresponse = types.ModuleType('cfnresponse')
    cfnresponse.SUCCESS, cfnresponse.FAILED = 'SUCCESS', 'FAILED'
    cfnresponse.send = lambda event, context, status, data, reason=None: sent.append((status, data, reason))
    boto3 = types.ModuleType('boto3')
    boto3.client = lambda service: types.SimpleNamespace(
        describe_subnets=lambda SubnetIds: {'Subnets': ec2(SubnetIds)})
    monkeypatch.setitem(sys.modules, 'cfnresponse', cfnresponse)
    monkeypatch.setitem(sys.modules, 'boto3', boto3)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    namespace = {}
    exec(inline_sources(template)[name], namespace)
    return namespace['handler'], sent


def test_pre_sign_up_lambda_filters_domains(template, monkeypatch):
    handler, _ = load_lambda(template, 'PreSignUpFunction', monkeypatch, env={
        'ALLOWED_DOMAINS': 'Example.com,@example.org', 'ADMIN_EMAIL': 'Boss@elsewhere.net'})

    def event(email):
        return {'request': {'userAttributes': {'email': email}}}

    for email in ('a@example.com', 'A@EXAMPLE.ORG', 'boss@elsewhere.net'):
        assert handler(event(email), None) == event(email)
    for email in ('a@sub.example.com', 'a@example.com.evil.net', 'a@evilexample.com', '', 'example.com'):
        with pytest.raises(Exception, match='approved email domains'):
            handler(event(email), None)


def test_subnet_zone_lambda(template, monkeypatch):
    subnets = [
        {'SubnetId': 'subnet-a', 'VpcId': 'vpc-1', 'AvailabilityZone': 'eu-west-1b', 'CidrBlock': '10.0.1.0/24'},
        {'SubnetId': 'subnet-b', 'VpcId': 'vpc-1', 'AvailabilityZone': 'eu-west-1a', 'CidrBlock': '10.0.2.0/24'}]
    handler, sent = load_lambda(template, 'SubnetZoneFunction', monkeypatch,
                                ec2=lambda ids: [s for s in subnets if s['SubnetId'] in ids])
    event = {'RequestType': 'Create', 'ResourceProperties': {
        'VpcId': 'vpc-1', 'SubnetIds': 'subnet-a,subnet-b'}}
    handler(event, None)
    status, data, reason = sent[-1]
    assert (status, reason) == ('SUCCESS', None)
    assert data['AvailabilityZone'] == 'eu-west-1b' and data['FirstSubnetId'] == 'subnet-a'
    assert data['SubnetIds'] == 'subnet-a,subnet-b'
    assert sorted(data['CidrBlocks'].split(',')) == ['10.0.1.0/24', '10.0.2.0/24']
    handler({**event, 'ResourceProperties': {**event['ResourceProperties'], 'VpcId': 'vpc-2'}}, None)
    assert sent[-1][0] == 'FAILED'
    handler({**event, 'ResourceProperties': {**event['ResourceProperties'], 'SubnetIds': 'subnet-a'}}, None)
    assert sent[-1][0] == 'FAILED'
    # Two subnets in one zone: the volume could not follow the instance.
    same_zone = subnets + [{'SubnetId': 'subnet-c', 'VpcId': 'vpc-1',
                            'AvailabilityZone': 'eu-west-1b', 'CidrBlock': '10.0.3.0/24'}]
    handler2, sent2 = load_lambda(template, 'SubnetZoneFunction', monkeypatch,
                                  ec2=lambda ids: [s for s in same_zone if s['SubnetId'] in ids])
    handler2({**event, 'ResourceProperties': {'VpcId': 'vpc-1', 'SubnetIds': 'subnet-a,subnet-b,subnet-c'}}, None)
    assert sent2[-1] == ('FAILED', {}, 'use one subnet per availability zone')
    # A delete must always succeed, whatever the properties.
    handler({'RequestType': 'Delete', 'ResourceProperties': {}}, None)
    assert sent[-1] == ('SUCCESS', {}, None)


def test_the_subnet_description_asks_for_one_subnet_per_zone(template):
    assert 'exactly one per availability zone' in template['Parameters']['PublicSubnetIds']['Description']


def test_subnet_zone_reads_the_parameters_and_feeds_the_existing_vpc_branches(template):
    resource = template['Resources']['SubnetZone']
    assert resource['Condition'] == 'ExistingVpc'
    props = resource['Properties']
    assert props['VpcId'] == {'Ref': 'VpcId'}
    assert props['SubnetIds'] == {'Ref': 'PublicSubnetIds'}
    resources = template['Resources']
    alb = resources['LoadBalancer']['Properties']['Subnets']['Fn::If']
    assert alb[2] == {'Fn::Split': [',', {'Fn::GetAtt': 'SubnetZone.SubnetIds'}]}
    asg = resources['AutoScalingGroup']['Properties']['VPCZoneIdentifier']
    assert asg == [{'Fn::If': ['CreateVpc', {'Ref': 'SubnetA'}, {'Fn::GetAtt': 'SubnetZone.FirstSubnetId'}]}]
    volume = resources['DataVolume']['Properties']['AvailabilityZone']['Fn::If']
    assert volume[2] == {'Fn::GetAtt': 'SubnetZone.AvailabilityZone'}
    sub = resources['LaunchTemplate']['Properties']['LaunchTemplateData']['UserData']['Fn::Base64']['Fn::Sub']
    assert sub[1]['TrustedProxies']['Fn::If'][2] == {'Fn::GetAtt': 'SubnetZone.CidrBlocks'}


def test_subnet_zone_role_is_least_privilege(template):
    role = template['Resources']['SubnetZoneRole']['Properties']
    (policy,) = role['Policies']
    (statement,) = policy['PolicyDocument']['Statement']
    assert statement['Action'] == 'ec2:DescribeSubnets'


# ------------------------------------------------------------------ neutrality

@pytest.mark.parametrize('path', [TEMPLATE, README], ids=lambda p: p.name)
def test_nothing_names_our_account_or_region(path):
    body = path.read_text()
    assert 'agenticml.xyz' not in body
    assert 'us-east-2' not in body
    assert not re.search(r'(?i)\becr\b|\.dkr\.', body)
    assert not re.search(r'(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])', body)


def test_template_has_no_email_address(text):
    assert not re.findall(r'[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+', text)


def test_template_has_no_hard_coded_region_or_partition(text):
    assert not re.search(r'\b(us|eu|ap|sa|ca|me|af)-[a-z]+-[0-9]\b', text)
    assert not re.search(r'arn:aws:', text)
