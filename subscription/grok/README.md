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
The CLI's own inspection can find extensions and default bundled skills. The
candidate permits explicitly identified built-in agents only when subagents
are disabled, and conservatively rejects other unexpected or unknown entries;
some legitimate setups may therefore be rejected. Inspection metadata is
marked unverified. Only one CLI version's flags informed this candidate.
The user must explicitly select and verify the official executable and model.

Important limitations before release: this implementation uses temporary task
files; Windows ACL protection has not been independently verified. It captures
inspection output in memory and does not enforce a streaming size cap. It does
not verify CLI binary authenticity, effective managed configuration, account
status, server-side effective effort, or per-request billing. A timed-out process can continue;
its task/output files are preserved and its status is `submission_uncertain`.
While this host process remains alive, its worker continues waiting on the same
original child and can collect the eventual outcome without a second request.
In-memory job tracking is lost when the hosting process exits and unknown jobs
remain uncertain. There is no automatic process kill or retry. One official
CLI version's help and a read-only inspection have been checked on Windows.
One short real task returned the expected answer. After subsequent code changes,
an attempted task stopped at preflight without a model request; a subsequent
inspection found bundled skills not explicitly disabled. A different new task then returned the
expected answer after those skills were disabled in an exclusive test profile.
The latest exact official session summary and usage records together reported
`grok-4.7`, `grok-4.7-build`, and `xhigh`.
The coordinator separately checked those records against its quality policy;
the earlier tested revision did not perform that check automatically. The damaged-ledger recovery paths were tested offline;
this successful task does not establish their behavior. Other CLI versions
have not been checked. This does not establish privacy, billing or cross-platform
compatibility. Do not use this candidate for real tasks until it has been
independently reviewed.

For a new successful, complete task, the adapter attempts to inspect only its
own UUID-bound official client session metadata and usage records. When those
records are safe, complete and consistent with the requested model and effort,
the coordinator exposes `client_record_verified: true` together with the
observed model and effort. This is an adapter-reported check; the coordinator
does not independently authenticate the record's origin. `verified` and
`server_effort_verified` remain false: these are local client records, not a
signed attestation of server execution.
In one isolated Windows MCP test on 2026-11-13 using Grok CLI 1.0.52, a synthetic task returned the expected
answer, and the tool reported `client_record_verified: true`, with client
records showing `grok-4.7` and `xhigh`. Server-side effort remains unverified;
one test does not establish compatibility with other client versions or setups.
The answer is saved before evidence is read; a later poll can retrieve updated
evidence, and missing or unsafe evidence leaves the answer usable but unverified.
If the final result cannot be committed to the local ledger, the adapter keeps
the task's output file for manual reconciliation; there is no automatic recovery
of that uncommitted result. It cannot promise all local copies
are removed. Protect the tool-owned state directory. This check does not
automatically retry the task. It never reads conversation
files or deletes session data; the official client may itself persist prompts,
answers, paths and sidecars, so use a private dedicated profile. Filesystem
permissions, unsupported platforms, unusually long encoded work paths, a CLI
format change, or missing/inconsistent records can prevent verification.

An inspection rejection retains the existing fixed error
`unexpected_or_unknown_extensions` and can additionally expose a fixed,
path-free `inspection_reason` such as `skills_present_or_unknown` or
`extensions_present_or_unknown`. It does not disclose skill names, paths, or
the inspection report. A missing disable marker is not proof a skill ran.
Users may separately configure `[skills].disabled` only in their own dedicated
profile and check that the official `inspect --json` reports `disabled: true`;
see the [official skills guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/08-skills.md).
The tool does not automatically change an existing or shared profile, and an
inspection snapshot cannot rule out later configuration changes. Other or
unknown extensions remain blocked; an original failed or uncertain task is
never automatically resent.

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

If the local request write fails before this adapter launches its worker, the
adapter only records a confirmed unstarted outcome when an atomic ledger update
proves no worker has claimed that reservation. If the update fails, it returns
an uncertain result with the original job ID and keeps the local slot. While
the same adapter remains alive, a later status check or submission can repeat
the conditional ledger update when storage recovers; it never restarts the
provider request or overwrites a claimed worker. If the adapter process exits
first, storage remains unavailable, or another worker claimed the job, the
reservation remains conservative rather than being released on an assumption.
The coordinator may reconcile that original receipt as not submitted only from
the built-in durable adapter's versioned ledger state, bound to the same task
and job. Older generic states and third-party adapter claims remain uncertain.
This local evidence assumes the tool-owned state directory has not been
tampered with; it is not a cryptographic or remote-provider attestation.
Every older generic `not_submitted` Grok ledger row is also counted against local
capacity, because earlier code could write that state after a competing worker
had claimed the job. Even a legacy row that really was never submitted cannot
be safely distinguished from that race from the row alone; old rows are not
changed or automatically released. Enough such rows can leave Grok locally
busy, and the current candidate does not provide an automatic reconciliation
command for them. Other providers can continue independently.

To avoid treating corrupt or unknown ledger records as finished, capacity
checks validate the local job history. This has not been load-tested with a
large ledger, and checks may slow as that history grows.
