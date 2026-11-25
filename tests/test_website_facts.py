"""Facts on the docs site (website/src/content/docs) that must match the code.

No browser and no Node: the Markdown files are read as text. Each test also
fails if the fact is not found at all, so a page rewrite cannot make a check
pass by dropping the sentence it guards.
"""
import json
import re
from pathlib import Path

from agenticml.server.clientwindow import DEFAULT_MIN_CLIENT

ROOT = Path(__file__).parents[1]
DOCS = ROOT / 'website/src/content/docs'
LIMITS = json.loads((ROOT / 'deploy/aws/limits.json').read_text(encoding='utf-8'))

# True: 3.0.0 is tagged and published (flipped in 29.9, which moved the
# raw.githubusercontent.com URLs to the v3.0.0 tag). Set it to False to check
# that no v3.0.0 raw URL is used before the tag exists.
PUBLISHED_3_0 = True


def _text(relative):
    return (DOCS / relative).read_text(encoding='utf-8')


def _gb(num_bytes):
    value = num_bytes / 1_000_000_000
    assert value == int(value), f'{num_bytes} bytes is not a whole number of GB'
    return int(value)


def test_free_plan_numbers_match_limits_json():
    """Protects the hosted pages from quoting free-plan limits that
    deploy/aws/limits.json no longer has (storage, daily upload, requests per
    minute, people, free workspaces per account)."""
    plan = LIMITS['plans'][LIMITS['default_plan']]
    storage = _gb(plan['storage_bytes'])
    upload = _gb(plan['upload_bytes_per_day'])
    rpm = plan['requests_per_minute']
    people = plan['people']
    workspaces = LIMITS['free_workspaces_per_account']

    expected = {
        'hosted/index.md': [
            rf'\| Teams \|.*up to {people} people on the free plan',
            rf'\| Storage \| {storage} GB of ',
            rf'\| Uploads \| {upload} GB per UTC day',
            rf'\| Requests \| {rpm} per minute per workspace',
            rf'\| People \| {people} \(members plus pending invites\)',
            rf'\| Free workspaces \| {workspaces} per account',
        ],
        'hosted/quickstart.md': [
            rf'of {storage}\.0 GB used',
            rf'the {upload} GB daily upload limit',
            rf'\(\d+ of {people}\)',
            rf'over {rpm} requests a minute',
        ],
        'hosted/workspaces.md': [
            rf'has up to {people} people',
            rf'\(\d+ of {people}\)',
            rf'how many of the {people} are used',
        ],
        'releases.md': [
            rf'{storage} GB storage, {upload} GB uploads per UTC day, '
            rf'{rpm} requests per minute per workspace, {people} people '
            rf'\(members plus pending invites\), and {workspaces} free '
            r'workspace per account',
        ],
        # Published policies: their limits must match deploy/aws/limits.json.
        'legal/terms.md': [
            rf'\*\*Workspaces:\*\* Each account can own {workspaces} free workspace',
            rf'\*\*Storage:\*\* {storage} GB \(decimal, ',
            rf'\*\*Daily uploads:\*\* {upload} GB of uploaded data',
            rf'\*\*Request rate:\*\* {rpm} requests per minute',
            rf'\*\*People:\*\* Up to {people} people',
        ],
        'legal/privacy.md': [
            rf'\({storage} GB storage, {upload} GB daily uploads, {rpm} requests per '
            rf'minute, {people} people\)',
        ],
    }
    for page, patterns in expected.items():
        text = _text(page)
        for pattern in patterns:
            assert re.search(pattern, text), f'{page}: no match for {pattern!r}'


def test_minimum_client_matches_default_min_client():
    """Protects the hosted quickstart and the Releases page from naming a
    minimum client version other than DEFAULT_MIN_CLIENT in
    src/agenticml/server/clientwindow.py."""
    version = re.escape(DEFAULT_MIN_CLIENT)
    expected = {
        'hosted/quickstart.md': [
            rf'needs client {version} or newer',
            rf'needs agenticml-lite >= {version}',
        ],
        'releases.md': [rf'requires client version {version} or newer'],
        'server/install.md': [rf'`AGENTICML_SERVER_MIN_CLIENT_VERSION` \| `{version}` \|'],
        'server/upgrade.md': [rf'`--min-client-version` \(default {version}\)'],
    }
    for page, patterns in expected.items():
        text = _text(page)
        for pattern in patterns:
            assert re.search(pattern, text), f'{page}: no match for {pattern!r}'
    # Any "X or newer" / ">= X" client statement on these pages must agree too.
    stated = set()
    for page in expected:
        text = _text(page)
        stated.update(re.findall(r'client(?: version)? (\d+(?:\.\d+)*) or newer', text))
        stated.update(re.findall(r'agenticml-lite >= (\d+(?:\.\d+)*)', text))
    assert stated == {DEFAULT_MIN_CLIENT}


def test_no_v3_0_0_raw_url_until_published():
    """Protects readers from raw.githubusercontent.com/.../v3.0.0/ URLs, which
    return 404 until the v3.0.0 tag exists (PUBLISHED_3_0 is flipped in 29.9)."""
    if PUBLISHED_3_0:
        return
    paths = [p for p in sorted(DOCS.rglob('*')) if p.suffix in ('.md', '.mdx')]
    paths += [ROOT / 'README.md', ROOT / 'docs/SERVER.md', ROOT / 'docs/HOSTED.md',
              ROOT / 'deploy/systemd/README.md']
    bad = [
        f'{path.relative_to(ROOT)}: {match}'
        for path in paths
        for match in re.findall(
            r'raw\.githubusercontent\.com/\S*?/v3\.0\.0/\S*', path.read_text(encoding='utf-8')
        )
    ]
    assert not bad, bad


# The published release tags. Update it after each release: a raw.githubusercontent.com
# URL in the docs must name one of these, or it returns 404.
PUBLISHED_TAGS = ('v3.0.0', 'v3.3.0', 'v3.3.1')

RAW_URL_DOC_FILES = [
    'README.md', 'docs/SERVER.md', 'docs/HOSTED.md', 'docs/PROTOCOL.md',
    'deploy/systemd/README.md', 'deploy/aws/README.md',
    'deploy/private-network/README.md', 'deploy/aws-self-hosted/README.md',
]
RAW_URL = re.compile(r'raw\.githubusercontent\.com/agenticML/agenticml-lite/([^/\s]+)/(\S*)')
TAG_ASSIGNMENT = re.compile(r'^\s*TAG=([^\s]+)', re.MULTILINE)


def test_raw_urls_use_published_tags():
    """Protects readers from raw.githubusercontent.com/agenticML/agenticml-lite/<ref>/
    URLs that return 404 because the tag does not exist, or that follow a branch
    and drift from the installed version. Every ref must be in PUBLISHED_TAGS.
    A `$TAG` ref is allowed when the same file sets `TAG=` to a published tag.
    Images under docs/assets/ are exempt: the README needs them on `main` so
    that PyPI shows them. Fails if no raw URL is found at all."""
    paths = [p for p in sorted(DOCS.rglob('*')) if p.suffix in ('.md', '.mdx')]
    for relative in RAW_URL_DOC_FILES:
        path = ROOT / relative
        assert path.is_file(), f'missing {relative}'
        paths.append(path)
    found = 0
    bad = []
    for path in paths:
        text = path.read_text(encoding='utf-8')
        where = path.relative_to(ROOT)
        for ref, rest in RAW_URL.findall(text):
            if rest.startswith('docs/assets/'):
                continue
            found += 1
            if ref in ('$TAG', '${TAG}'):
                if not TAG_ASSIGNMENT.search(text):
                    bad.append(f'{where}: {ref} used but the file has no TAG= line')
            elif ref not in PUBLISHED_TAGS:
                bad.append(f'{where}: {ref}/{rest}')
        bad.extend(
            f'{where}: TAG={tag}' for tag in TAG_ASSIGNMENT.findall(text)
            if tag not in PUBLISHED_TAGS
        )
    assert found, 'no raw.githubusercontent.com URL found in the docs'
    assert not bad, bad


def test_releases_page_names_the_package_version():
    """Protects the Releases page from naming an older version than the package.
    It fails after a version bump until the page is updated, on purpose: bump
    the version in the release PR together with the "Which version should I
    use?" table and the `## <version>` section."""
    match = re.search(r'^version = "(.*)"', (ROOT / 'pyproject.toml').read_text(encoding='utf-8'), re.MULTILINE)
    assert match, 'no version in pyproject.toml'
    version = match.group(1)
    text = _text('releases.md')
    rows = re.findall(r'^\| (?:Local tracking only|Self-hosted server|Hosted service) \| (\S+) from PyPI \|',
                      text, re.MULTILINE)
    assert len(rows) == 3, rows
    assert set(rows) == {version}, f'table names {rows}, package is {version}'
    section = re.search(rf'^## {re.escape(version)}\n\n(\*\*Status:\*\*[^\n]*)', text, re.MULTILINE)
    assert section, f'no "## {version}" section with a status line'
    assert section.group(1).startswith('**Status:** Released'), section.group(1)


# Pages that must install from PyPI, not from a git URL or the `dev` branch.
INSTALL_DOC_FILES = [
    'README.md', 'docs/SERVER.md', 'docs/HOSTED.md',
    'deploy/systemd/README.md', 'deploy/aws/README.md',
]
# The one deliberate git install: the 2.2 client of the compatibility check.
GIT_INSTALL_ALLOWED = 'agenticml-lite@bd0d499'


def test_no_git_install_remains_in_the_docs():
    """3.0.0 is on PyPI, so no doc installs it from GitHub or follows `dev`.
    Fails if a file it reads is missing."""
    paths = [p for p in sorted(DOCS.rglob('*')) if p.suffix in ('.md', '.mdx')]
    assert paths, 'no docs site pages found'
    for relative in INSTALL_DOC_FILES:
        path = ROOT / relative
        assert path.is_file(), f'missing {relative}'
        paths.append(path)
    git_install = re.compile(r'git\+https://github\.com/agenticML\S*|agenticml-lite@dev\b|@dev\b')
    bad = []
    for path in paths:
        for match in git_install.findall(path.read_text(encoding='utf-8')):
            if match.rstrip("'\"`)") == 'git+https://github.com/agenticML/agenticml-lite@bd0d499' \
                    and path == ROOT / 'deploy/aws/README.md':
                continue
            bad.append(f'{path.relative_to(ROOT)}: {match}')
    assert not bad, bad
    pin = (ROOT / 'deploy/aws/README.md').read_text(encoding='utf-8')
    assert GIT_INSTALL_ALLOWED in pin, 'the 2.2 client pin is gone; update this test'


def _login_key_examples(text):
    """Return `agenticml login ... --key` examples: lines in code fences
    (backslash continuations joined) and inline code spans. Prose that merely
    warns against --key, outside any code span on the login command, is not
    an example."""
    found = []
    in_fence = False
    fence_lines = []
    for line in text.split('\n'):
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            fence_lines.append(line)
    fenced = re.sub(r'\\[ \t]*\n', ' ', '\n'.join(fence_lines))
    found += re.findall(r'login[^\n]*--key[^\n]*', fenced)
    outside = '\n'.join(
        line for line in text.split('\n') if not line.lstrip().startswith('```')
    )
    found += re.findall(r'`[^`\n]*login[^`\n]*--key[^`\n]*`', outside)
    return found


def test_no_login_with_key_on_the_command_line():
    """Protects readers from `agenticml login ... --key <value>` examples: the
    value shows in `ps`. `agenticml export --key` is a metric filter and is not
    matched."""
    paths = [p for p in sorted(DOCS.rglob('*')) if p.suffix in ('.md', '.mdx')]
    paths.append(ROOT / 'README.md')
    bad = [
        f'{path.relative_to(ROOT)}: {example}'
        for path in paths
        for example in _login_key_examples(path.read_text(encoding='utf-8'))
    ]
    assert not bad, bad


def test_login_key_detector_catches_examples():
    """Protects the check above from going blind: the detector must flag a
    fenced example, a continued fenced example and an inline example, and
    ignore the export filter and prose that only warns about --key."""
    assert _login_key_examples('```bash\nagenticml login --host h --key abc\n```')
    assert _login_key_examples('```bash\nagenticml login --host h \\\n  --key abc\n```')
    assert _login_key_examples('```bash\nagenticml login --host h \\  \n  --key abc\n```')
    assert _login_key_examples('Run `agenticml login --key abc` now.')
    assert not _login_key_examples('```bash\nagenticml export --key loss\n```')
    assert not _login_key_examples('Use the `agenticml login` prompt. Avoid `--key`.')


def test_private_network_page_has_the_key_facts():
    """Protects readers of the private-network page: the client flag and
    variable, the proxy body limit and the minimum version must be on it."""
    text = _text('server/private-network.md')
    for needle in ('--ca-bundle', 'AGENTICML_CA_BUNDLE', 'client_max_body_size 4g', 'agenticml-lite>=3.2'):
        assert needle in text, f'server/private-network.md does not mention {needle}'


def test_deploy_on_aws_page_has_the_key_facts():
    """Protects readers of the Deploy on AWS page: the template path, every
    template parameter, the restore snapshot parameter and the commands that
    set up the data directory must be on it, and no key goes on a command line."""
    import yaml
    from test_deploy_cloudformation import CfnLoader

    template = yaml.load((ROOT / 'deploy/aws-self-hosted/agenticml.yaml').read_text(), Loader=CfnLoader)
    parameters = sorted(template['Parameters'])
    assert 'DataSnapshotId' in parameters
    page = 'server/deploy-on-aws.md'
    text = _text(page)
    needles = [
        'deploy/aws-self-hosted/agenticml.yaml', *parameters, 'DataSnapshotId',
        '/etc/agenticml-server.env', 'install -d -o agenticml', 'sudo -u agenticml',
    ]
    missing = [needle for needle in needles if needle not in text]
    assert not missing, f'{page} does not mention {missing}'
    assert not _login_key_examples(text), _login_key_examples(text)


def test_contact_address_is_the_same_everywhere():
    """Protects the Contact us button: the address in the server default, the docs
    site button, the hosted template and the Terms page must be one address."""
    import yaml
    from agenticml.server.webauth import DEFAULT_CONTACT_EMAIL
    from test_deploy_cloudformation import CfnLoader

    terms = re.search(r'\[(\S+@\S+?)\]\(mailto:', _text('legal/terms.md'))
    assert terms, 'legal/terms.md has no mailto contact link'
    address = terms.group(1)
    button = (ROOT / 'website/src/components/ContactButton.astro').read_text(encoding='utf-8')
    constant = re.search(r"CONTACT_EMAIL = '([^']+)'", button)
    assert constant, 'ContactButton.astro has no CONTACT_EMAIL constant'
    hosted = yaml.load((ROOT / 'deploy/aws/cloudformation/hosted.yaml').read_text(), Loader=CfnLoader)
    assert DEFAULT_CONTACT_EMAIL == address
    assert constant.group(1) == address
    assert hosted['Parameters']['ContactEmail']['Default'] == address


def test_hosted_keys_page_has_the_key_facts():
    """Protects readers of the hosted sign-in and sync keys page: the
    variables, the credentials file, the login and whoami commands, the web
    session limits (from agenticml.server.webauth) and the key name length
    limit (from agenticml.server.keys) must be on it and match the code, and
    no key goes on a command line."""
    from agenticml.server.keys import _NAME
    from agenticml.server.webauth import SESSION_IDLE, SESSION_MAX_AGE

    page = 'hosted/keys.md'
    text = _text(page)
    needles = [
        'AGENTICML_API_KEY', 'AGENTICML_HOST', 'AGENTICML_CONFIG_DIR',
        'credentials.json', 'agenticml login --host https://app.agenticml.xyz',
        'agenticml whoami',
    ]
    missing = [needle for needle in needles if needle not in text]
    assert not missing, f'{page} does not mention {missing}'

    assert SESSION_IDLE % 3600 == 0, f'SESSION_IDLE={SESSION_IDLE} is not a whole number of hours; update this test'
    assert SESSION_MAX_AGE % 86400 == 0, f'SESSION_MAX_AGE={SESSION_MAX_AGE} is not a whole number of days; update this test'
    idle = f'{SESSION_IDLE // 3600} hours'
    max_age = f'{SESSION_MAX_AGE // 86400} days'
    assert idle in text, f'{page} does not state the web session idle limit "{idle}" (SESSION_IDLE)'
    assert max_age in text, f'{page} does not state the web session maximum age "{max_age}" (SESSION_MAX_AGE)'

    parsed = re.search(r'\{1,(\d+)\}', _NAME.pattern)
    assert parsed, f'cannot read the maximum length from _NAME.pattern {_NAME.pattern!r}; update this test'
    length = f'up to {int(parsed.group(1))} characters'
    assert length in text, f'{page} does not state the key name length limit "{length}" (_NAME in server/keys.py)'

    assert not _login_key_examples(text), _login_key_examples(text)
