# tealc/optimizer: private fork of evcc-io/optimizer

Read @AGENTS.md for the project itself. This file only covers the fork.

## Two repos, one system

The work spans two forks: this optimizer and the evcc fork (`tealc/evcc`, branch `custom`), which is the optimizer's only client. Before building a change, decide which side it belongs to: what depends on the UI, the user's language or evcc's own state is evcc's job, what depends on the model, the solver or the deployment is the optimizer's. A setting here is a fallback, not a substitute for sending the right data from evcc. Say which side you picked and why, and attach `tealc/evcc` with `add_repo` when the change may belong there.

## Branches

- `main` mirrors `evcc-io/optimizer` main. Never commit to it.
- `custom` = upstream main + the fork commits below. This is what gets built and deployed.

## Fork commits on `custom`

1. `feat: plan around device availability and energy used away`
   - New optional per-battery fields in `BatteryConfig`: `c_avail` (0/1 per slot, 0 = device not connected/away) and `e_away` (Wh used per slot while away, e.g. driving).
   - Touches `src/optimizer/optimizer.py`, `app.py`, `explain.py`, `openapi.yaml`, `client/client.gen.go`, tests in `tests/test_availability.py`, `test_cases/031*`, `032*`.
   - Without the fields the model behaves exactly like upstream.
   - Consumer: the evcc fork (`tealc/evcc`, branch `custom`) sends these fields when a vehicle has absence windows.
2. `chore: fork image build and agent notes`: `.github/workflows/fork-image.yml` and this file.
3. `feat: explain plans with the Claude API`
   - `OPTIMIZER_ANTHROPIC_API_KEY` (and `OPTIMIZER_ANTHROPIC_MODEL`, default `claude-haiku-5-5`) switch `/optimize/explain` from Azure OpenAI to the Anthropic Messages API. Without the key Azure is used as upstream.
   - Touches `src/optimizer/explain.py` (`claude_chat()`, `configured()`), `settings.py`, `app.py`, `README.md`, tests in `tests/test_explain.py`.

Keep fork commits few and focused, so rebasing stays cheap. New fork work gets its own commit. Fixes to an existing fork feature are folded into that commit: `git commit --fixup=<sha>`, then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash upstream/main`.

## Syncing with upstream

```
git remote add upstream https://github.com/evcc-io/optimizer   # once
git fetch upstream main
git push origin upstream/main:main                              # update the mirror
git rebase upstream/main custom
uv run --frozen pytest -q                                       # all must pass
git push --force-with-lease origin custom
```

- On conflicts keep upstream's intent and re-apply the availability feature on top. Renumber `test_cases/031*`/`032*` if upstream adds files with the same numbers.
- `tests/test_peak_leveling.py::test_the_long_horizon_case_still_levels_once_the_solve_splits` is flaky upstream when run alone (solver time limit); it passes in the full suite.

## Build and deploy

- A push to `custom` runs `.github/workflows/fork-image.yml`: the test suite, then it publishes `ghcr.io/tealc/optimizer:custom` (amd64 only, the Dockerfile downloads an x86_64 CBC).
- The NAS runs it as the optimizer for the evcc fork, evcc points to it with `OPTIMIZER_URI`.
