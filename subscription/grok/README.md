# Candidate: Grok official CLI adapter

This candidate runs the official Grok CLI under the user's own login, not the
xAI HTTP API. The user chooses the CLI executable, a dedicated Grok profile,
model and effort, then runs `login()` interactively. No API key is requested,
read or intentionally inherited. Existing model/account settings, usage and
charges remain the user's responsibility. A cached login and this adapter do
not prove the billing method: system policy, managed configuration or CLI
behavior may still alter routing. The CLI itself may retry for authentication;
the adapter does not automatically resubmit an uncertain task or switch APIs.

`submit(task, task_id)` runs a preflight and one background request; `poll`
returns its state. By default this adapter allows two local Grok jobs at once;
this is a local bound, not a statement about official account capacity. A full
adapter returns `not_submitted/provider_busy`; another provider is unaffected.
The CLI's own inspection can find extensions and default
bundled skills: this candidate rejects every nonempty list and may therefore
reject legitimate setups. Only one CLI version's flags informed this candidate.
The user must explicitly select and verify the official executable and model.

Important limitations before release: this implementation uses temporary task
files; Windows ACL protection has not been independently verified. It captures
inspection output in memory and does not enforce a streaming size cap. It does
not verify CLI binary authenticity, effective managed configuration, account
status, model/effort, or per-request billing. A timed-out process can continue;
its task/output files are preserved and its status is `submission_uncertain`.
While this host process remains alive, its worker continues waiting on the same
original child and can collect the eventual outcome without a second request.
In-memory job tracking is lost when the hosting process exits and unknown jobs
remain uncertain. There is no automatic process kill or retry. Real login,
official CLI compatibility, Windows behavior, privacy and subscription billing
have not been tested. Do not publish or use with real tasks until these issues
are resolved and independently reviewed.

`durable_grok.py` provides a separate detached-worker candidate using an
explicit, existing tool-owned state directory. It reserves each task in SQLite
before launching, runs one child worker, and can query the original job ID after
the launcher exits. Task and output are stored under that directory, protected
only by its operating-system permissions; this code does not independently
verify Windows ACLs or guarantee isolation against other users/processes. It
does not copy or inspect the user's login credentials or provider configuration.
On successful worker intake, its own `request.json` is removed; successful output
and job metadata remain in the SQLite database until the user removes the
tool-owned state directory. If a worker crashes before intake or a process has
not been confirmed to exit, its own files can remain. Do not delete that job's
files until its original worker and child have finished. There is no automatic
retry, automatic recursive cleanup, or mechanism to recover the result after an
unconfirmed worker crash. Local concurrency is counted in this ledger and does
not represent an official provider quota. Synthetic subprocess tests do not
verify actual CLI, authentication, billing, filesystem ACLs, or provider policy.
