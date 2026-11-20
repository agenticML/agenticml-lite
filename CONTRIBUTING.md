# Contributing to agenticml-lite

Thanks for helping. This page covers how to set up a development environment, run the tests, send a pull request, and make a release.

Security issues: see [SECURITY.md](SECURITY.md). Do not open a public issue for them.

## Development setup

From a clone of the repository:

```bash
pip install -e ".[dev]"
pytest
python scripts/bench.py --quick
```

`pytest` runs the unit tests in `tests/`. `scripts/bench.py --quick` is a quick performance check; its output is described under "Performance" on the [storage page](https://agenticml.github.io/agenticml-lite/reference/storage/#performance).

The `testbed/` directory is a small user project that consumes `agenticml-lite`, with CLI and dashboard tests. Set up its virtual environment once (see [testbed/README.md](testbed/README.md) for the full guide):

```bash
cd testbed
uv venv --python 3.10
uv pip install -e '..[dev]' playwright==1.62.0
uv pip install -e '.[test]'
# pip equivalent, after creating/activating a venv:
# python -m pip install -e '..[dev]' playwright==1.62.0 -e '.[test]'
.venv/bin/python -m playwright install chromium   # for the dashboard tests
```

## Tests to run before a pull request

From the repository root:

```bash
testbed/.venv/bin/python -m pytest -q tests
```

From `testbed/`, for any dashboard change:

```bash
.venv/bin/python -m pytest -q -m ui
```

Dashboard changes must keep zero horizontal page overflow at a 375px viewport width.

macOS is not a CI target.

## Branches and pull requests

- Open pull requests against `main`.
- Releases are `v*` tags on `main`.
- Keep a pull request to one change, and say in the description which tests you ran. The pull request template has a checklist.
- If a change touches the dashboard, say that you checked it at 375px.

## Releasing

Maintainers only. Bump the version in `pyproject.toml` and `src/agenticml/__init__.py`, then push a `v*` tag.

- **TestPyPI dry run:** run the "Publish to TestPyPI" workflow by hand from the Actions tab. Re-running with a version that is already there is a no-op.
- **PyPI:** `publish.yml` publishes to PyPI with Trusted Publishing (no API tokens) when the tag matches the package version.

```bash
# bump version in pyproject.toml and src/agenticml/__init__.py
git commit -am "release: vX.Y.Z"
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Conduct

Be kind and constructive. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
