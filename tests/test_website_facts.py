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

# False until 3.0.0 is tagged and published; flipped in 29.9, which moves the
# raw.githubusercontent.com URLs back to the tag.
PUBLISHED_3_0 = False


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
    bad = [
        f'{path.relative_to(ROOT)}: {match}'
        for path in sorted(DOCS.rglob('*'))
        if path.suffix in ('.md', '.mdx')
        for match in re.findall(
            r'raw\.githubusercontent\.com/\S*?/v3\.0\.0/\S*', path.read_text(encoding='utf-8')
        )
    ]
    assert not bad, bad


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
    fenced = re.sub(r'\\\n', ' ', '\n'.join(fence_lines))
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
    assert _login_key_examples('Run `agenticml login --key abc` now.')
    assert not _login_key_examples('```bash\nagenticml export --key loss\n```')
    assert not _login_key_examples('Use the `agenticml login` prompt. Avoid `--key`.')
