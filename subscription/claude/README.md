# Claude Code text adapter: local development candidate

This unpublished candidate launches the user's installed, unmodified official
Claude Code CLI only after explicit enablement and an official account-status
check. Users sign in using the CLI itself and choose their own model and effort.
The adapter does not ask for, read or supply API keys, and does not copy login
credentials. It removes API environment variables from child processes; it does
not modify existing shell settings or the original collaboration system.

Account, subscription and billing settings remain under the user's control.
Login status does not prove the eventual billing route or rule out extra usage
charges. The CLI, managed settings and account configuration can affect billing.
No automatic fallback to an API key or alternate model is implemented here.

Use dedicated directories supplied by the user for the CLI profile and work
space. The CLI can read and write its own files there. This implementation does
not itself read the profile or a credentials file, but cannot guarantee that
the CLI will never use system-managed configuration or other OS facilities.

Jobs live in memory. A lost process means status is uncertain, not permission to
resubmit. The coordinator must reserve IDs persistently and avoid automatic
retries. A slow model process first reports `submission_uncertain`, remains
running, and can later return a result from that same process. The warning
threshold does not kill the process. The in-process worker is not a durable
supervisor: closing or crashing the parent can interrupt monitoring and make the
result unavailable; this has not been tested against a real provider. Process
exit and response text do not prove model or reasoning effort; those fields are
explicitly marked unverified.

On Windows, one short task using this candidate returned the expected answer
after subsequent code changes. The candidate reported quality as unverified;
no official record was obtained to independently confirm the actual model or
effort for that task. This does not establish reliability on other systems,
accounts or tasks.

The local adapter defaults to at most two active Claude jobs; callers can choose
another bounded local limit. A busy adapter reports `not_submitted`, and replay
of an existing task ID does not start a second job. This local limit is not a
statement about any Anthropic plan, quota, or official concurrency limit. A slow
Claude job consumes one Claude slot, not a Grok slot.

`durable_claude.py` is a separate candidate that starts one local background
worker per accepted job and stores a SQLite receipt in a user-provided state
directory. Prompts pass through an unnamed pipe and are not written to its
ledger; a deterministic task/model/effort hash is stored for replay detection
and should be treated as sensitive. Successful result text is stored in a job-specific result file in that
directory. Configure an existing private directory with suitable OS permissions;
the candidate does not verify Windows ACLs and cannot guarantee that the CLI or
system tools store no other local data. Results and the ledger are retained
until the user removes them. After saving a completed answer, the user may
delete its corresponding `<job_id>.result.json`; doing so makes that answer
unavailable from this adapter. Keep the ledger to preserve replay protection.
Delete the entire state directory only after every job is complete or manually
reconciled and its results are no longer needed; never remove active or unknown
jobs as a way to retry them.

The local detached-worker test has only checked a synthetic Python process on
the test machine. Some operating-system or host job policies may terminate a
child when its parent exits; process loss stays uncertain and does not prove a
task was never sent. Real provider recovery and private-directory permissions
have not been verified.

All included tests are offline and mock or replace the Claude CLI. No real
provider task has been run. Anthropic's human clarification of public third-party
orchestration is pending; this is not a public release and has not been approved
for distribution.
