# Do Again: non-browser Codex transport (experimental)

The Chrome transport is not true headless when it resolves to `background`.
On the October 10, 2026 Mac acceptance attempt, a **disposable true-headless
Chrome profile** reached ChatGPT's human-verification page, not an authenticated
composer. The user-facing Chrome window is therefore not an accepted headless
solution. Do not bypass the verification challenge.

## Signed-in, non-browser alternative

The supported [Codex CLI](https://developers.openai.com/codex/) has a
non-interactive `codex exec` mode and can authenticate with a ChatGPT account.
Model usage is subject to applicable Codex plan allowances and limits.

An isolated, pinned Codex CLI `0.162.1` was installed for the Mac operator at:

```text
~/.do_again/codex-tools/node_modules/.bin/codex
```

This installation does not overwrite any global Node package, ChatGPT Chrome
profile, launchd service, protected supervisor, or project. It is **not
authenticated automatically**. On October 10, 2026, the user completed device
sign-in and a private, read-only Mac workflow verified the installed CLI is
signed in without making a model call. To reauthenticate deliberately, from a
local terminal under the same macOS operator account:

```bash
"$HOME/.do_again/codex-tools/node_modules/.bin/codex" login --device-auth
```

Device authentication may ask you to open a browser yourself to complete
one-time authorization. There is no autonomous visible Chrome interaction.
Never print, share, or check in authentication tokens or device codes.

Once a future approved Do Again release containing this branch is installed:

```bash
do-again model status
do-again model smoke --allow-model-call
```

A third command prepares, but **does not publish or execute**, exactly one
synthetic edit proposal:

```bash
do-again model canary-draft --nonce <fresh-24-hex-nonce> --task 1 \
  --head <exact-40-character-commit-SHA> --allow-model-call
```

It produces a typed JSON `scratch_script` request for offline review, using
the source and tests authored by one signed-in Codex model call. Invalid
Python, imports, file names, source heads or capabilities fail closed. The
draft is **not** a grant, and its output must not be treated as a broker
acknowledgment, CI evidence or authorization to activate the canary.

The status command does not use model inference. The smoke command uses
**one explicitly authorized inference** against the signed-in Codex allowance,
is restricted to a read-only sandbox, emits only a bounded structured result,
and does not open a Chrome window or touch GitHub.

The `src/do_again/model_transport.py` adapter is intentionally opt-in:
`generate_structured` blocks without `allow_model_call=True`, requires an
authenticated Codex CLI, bounds prompt/schema/output sizes, never retries an
ambiguous result, and exposes neither model error logs nor credentials. It
forces `--sandbox read-only` and `--ask-for-approval never`.

## Acceptance boundaries

**This is a transport foundation, not completed autonomous release
qualification.** At this stage it returns structured model output only.
An additional pure offline boundary, `model_canary.prepare_canary_edit`,
translates a proposed implementation and unittest into the existing typed,
head-fenced `scratch_script` request. It checks the exact nonce, task index,
repository head, Python syntax, module imports, approved calls and output paths;
it creates no file, browser, GitHub, or model effects. It is currently a
request **candidate**, not an authorized publication or execution.

It does **not** publish Do Again control requests, dispatch protected
work, drive a two-task canary, or replace the conversation/acknowledgment
protocol. The installed broker's grant is still tied to the original
ChatGPT conversation and must not be silently repurposed.

Before using Codex for unattended development, a future review must bind a
new signed transport identity to the sealed broker, enforce one-shot
GitHub request publication and durable receipt acknowledgments, provide
bounded model-context and usage budgets, and prove the two-task synthetic
canary on native macOS. Any uncertain inference or publication must halt
without replay. Until then:

- Production remains disabled.
- The Do Again and jobpipe parent projects stay in maintenance.
- Sonary is outside scope.
- No real job applications may be submitted.
- The previously ambiguous browser submission is never replayed.
