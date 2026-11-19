# Fix: admin email addresses printed in the public deploy log

Maintenance fix, found on Nov 19, 2026 while reading the hosted deploy run
44499848598 (release 3.1 deploy). The "Deploy hosted stack" step's env block
listed `ADMIN_EMAILS` with the owner's real address in plain text. The
repository is public, so anyone can read every Actions run log.

## Goal

Future deploy logs never show the admin email addresses. The value moves from
the `hosted` environment **variable** `HOSTED_ADMIN_EMAILS` (printed in every
step's env block) to an environment **secret** of the same name (GitHub masks
secrets as `***`). A deploy must never silently drop the admin list, so the
workflow refuses to run while the old variable still exists.

The Firebase web API key in the same env block is public by design and stays a
variable.

## Files to touch

- `.github/workflows/deploy.yml`
- `tests/test_deploy_cloudformation.py`
- `deploy/aws/README.md`
- `docs/HOSTED.md`
- `docs/plans/on-prem-roadmap.md` (Maintenance fixes row, Backlog owner step;
  supervisor)
- this plan

## Approach

1. **Check step.** In "Check required variables", add to the step's `env`:
   `ADMIN_EMAILS_VARIABLE_SET: ${{ vars.HOSTED_ADMIN_EMAILS != '' }}`. The
   expression renders only `true` or `false`, so the log never shows the
   value. In `run`, when it is `true`, print an `::error::` that says the
   `hosted` environment still has a `HOSTED_ADMIN_EMAILS` variable, that run
   logs of this public repository print variables, and to create an
   environment secret `HOSTED_ADMIN_EMAILS` with the same value and delete the
   variable (name the two `gh` commands); then `exit 1`. This runs before any
   AWS step.
2. **Deploy step.** Change `ADMIN_EMAILS: ${{ vars.HOSTED_ADMIN_EMAILS }}` to
   `ADMIN_EMAILS: ${{ secrets.HOSTED_ADMIN_EMAILS }}`. Right after
   `set -euo pipefail`, mask each comma-separated address on its own (GitHub
   masks only the whole secret string, so an address printed alone, for
   example in a CloudFormation error, would otherwise show):
   ```bash
   IFS=',' read -r -a admin_emails <<< "$ADMIN_EMAILS"
   for address in "${admin_emails[@]}"; do
     if [ -n "$address" ]; then
       echo "::add-mask::$address"
     fi
   done
   ```
   with a short comment. Everything else in the step stays as is.
3. **Tests** (`tests/test_deploy_cloudformation.py`):
   - Rename `test_deploy_workflow_passes_the_admin_emails_variable_as_a_parameter_override`
     to `..._admin_emails_secret_...` and assert the env value is
     `${{ secrets.HOSTED_ADMIN_EMAILS }}`.
   - New `test_deploy_workflow_masks_each_admin_email_before_the_deploy`: the
     deploy step's `run` contains `::add-mask::` and the masking comes before
     `aws cloudformation deploy`.
   - New `test_deploy_workflow_reads_the_admin_emails_variable_only_as_a_flag`:
     every occurrence of `vars.HOSTED_ADMIN_EMAILS` in the workflow text is
     followed by ` != ''` (regex), and there is at least one.
   - New behaviour test: run the "Check required variables" step's `run`
     block with `bash` (`subprocess.run(['bash', '-c', block], env=...)`) with
     both role ARNs set: `ADMIN_EMAILS_VARIABLE_SET=true` exits non-zero and
     prints `HOSTED_ADMIN_EMAILS`; `false` exits 0. Skip if `bash` is missing.
   - New behaviour test of the masking: run `bash -c` on the part of the
     deploy step's `run` before `overrides=(` (taken from the workflow, not
     copied into the test) with
     `ADMIN_EMAILS=a@example.com,b@example.com` and check stdout has exactly
     `::add-mask::a@example.com` and `::add-mask::b@example.com`; with
     `ADMIN_EMAILS=` it prints nothing and exits 0. Use `example.com`
     addresses in the test file only.
   - `test_no_email_address_in_deploy_workflow` and
     `test_deploy_workflow_run_blocks_take_values_from_env_only` keep passing
     unchanged.
4. **Runbook** (`deploy/aws/README.md`):
   - Environment setup: remove the `gh variable set HOSTED_ADMIN_EMAILS` line;
     add a short "Environment secret" step:
     `gh secret set HOSTED_ADMIN_EMAILS --env hosted` (it prompts for the
     value, so it stays out of shell history), with one sentence on why: run
     logs of this public repository print variables; secrets show as `***`.
     GitHub cannot show a secret's value again, so keep the list somewhere
     safe.
   - Move the `HOSTED_ADMIN_EMAILS` row out of the variables table into a
     one-row secrets note/table, and say the deploy stops at "Check required
     variables" while a `HOSTED_ADMIN_EMAILS` *variable* exists.
   - Add "Moving the admin emails from a variable to a secret" (existing
     deployments): `gh secret set HOSTED_ADMIN_EMAILS --env hosted` with the
     same value, then `gh variable delete HOSTED_ADMIN_EMAILS --env hosted`,
     before the next deploy. Old run logs still contain the address; to remove
     it, delete those runs' logs (`gh run list --workflow deploy.yml`, then
     `gh api -X DELETE repos/<owner>/<repo>/actions/runs/<id>/logs`, or
     delete the runs in the Actions tab).
   - Elsewhere where the README calls `HOSTED_ADMIN_EMAILS` a variable
     (around lines 302, 372 and the "configuration changes" list near 952),
     say secret. Keep the rest of the wording.
5. **`docs/HOSTED.md`** line 59: "GitHub environment variable" → "GitHub
   environment secret".

## Constraints and conventions

- Actions stay pinned by SHA; no new actions.
- `run` blocks take values from `env` only (no `${{` inside `run`).
- No real email address anywhere in the repo; tests use `example.com`.
- Keep comments short and in the existing style.

## Edge cases

- Secret unset or empty: `ADMIN_EMAILS` is empty, the loop masks nothing,
  and the template gets `AdminEmails=` as today (no admins from the list).
- Both secret and variable set: the check fails until the variable is
  deleted (the value would be printed otherwise).
- Merging this to `dev` queues a deploy (the workflow file changed). If the
  owner has not moved the value yet, that run stops at the check step after
  approval, before any AWS call; nothing changes on the stack.

## Tests and checks

- `testbed/.venv/bin/python -m pytest -q tests/test_deploy_cloudformation.py`
- `testbed/.venv/bin/python -m pytest -q tests`
- `grep -n "vars.HOSTED_ADMIN_EMAILS" .github/workflows/deploy.yml` shows
  only the `!= ''` flag.

## Done when

The workflow reads the admin emails from the secret and masks each address,
the check step refuses a leftover variable, the tests above pass, the runbook
describes the secret, and a PR to `dev` is open. Moving the value to a secret
and deleting old run logs are owner steps in the roadmap Backlog.
