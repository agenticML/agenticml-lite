# Fix: empty object and lineage upload framing

Status: done (2.2 PR)

## Goal

Fix the package 3 sync client regression where requests adds chunked encoding
to an empty streamed object, causing the strict server to return HTTP 411.
Apply the same guard to package 7 lineage uploads.

## Files to touch

- `src/agenticml/client.py`: use an empty bytes body with no rewind for size 0.
- `tests/test_client.py`: verify HTTP framing for empty and non-empty object
  and lineage uploads, including retries.
- `tests/test_sync.py`: sync a run referencing an empty object to the real server.
- `docs/PROTOCOL.md`: clarify empty object framing if not already explicit.
- `docs/plans/on-prem-roadmap.md`: track this follow-up's status.

## Approach

1. Add HTTP regression coverage using the existing local test server and real
   file handles, plus a real-server sync regression for empty tracked content.
2. Confirm the new tests reproduce the framing failure.
3. For both upload methods, send `data=b""` and omit rewind when `size == 0`.
   Keep non-empty uploads streaming with their existing length and retry logic.
4. Document the required `Content-Length: 0` without chunked encoding.
5. Run focused regressions, then the full suite from the repository root.

## Constraints and edge cases

Keep the server strict and unchanged. Preserve non-empty uploads, explicit
Content-Length, timeouts, retry behavior, and return values. An empty upload
must remain correctly framed on retries. Do not commit.

## Tests and checks

Use the existing `fake_http` fixture to check received bodies, Content-Length,
and absence of Transfer-Encoding for both methods. Exercise empty code and
artifact references through the real sync server and verify the server object
and completed sync state. Run:

`PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python -m pytest -q tests`

## Done when

Empty object sync succeeds, empty and non-empty upload framing tests pass,
retry tests pass, and the full suite passes with no server changes.

## Verification

- Before the fix: both empty upload framing cases and both real-server empty
  object sync cases failed; non-empty framing cases passed.
- After the fix: client and sync suites passed (106 tests).
- Full command above: 2220 passed, 9 skipped in 220.31 seconds.
- `git diff --check` passed. No server code changes or commits.
