# Local collaboration core

This directory contains the provider-independent coordinator and MCP transport.
`mcp_stdio.py --db ...` on its own loads no provider adapters and makes no real
provider requests. An application must inject separately configured adapters.
The unit tests use fake adapters; they do not prove live provider connectivity,
permissions, model identity, subscriptions, or billing.

The isolated core offers a SQLite at-most-once reservation and a minimal MCP
stdio front end. The database stores request-key hashes, task IDs, provider,
status and adapter job IDs. It does not persist task text or response text.
Users must choose the database path; its directory permissions and lifecycle
are their responsibility. Request keys and adapter job IDs can themselves
contain sensitive information, so do not place secrets or private text in
those fields. A locally running process may still see task text in memory.

An adapter object may be injected by application code and provides
`submit(task=..., task_id=...) -> dict` and repeatable, read-only `poll(job_id)`.
Submit must return `queued`/`running` with `job_id`, `not_submitted`, or
`submission_uncertain`. Poll returns one of those states or a final status,
and successful output must be a string. A poll cannot turn a known job receipt
into `not_submitted`: that observation remains uncertain. The adapter is
responsible for retaining results for repeated reads. The core does not store
output text; if a provider's in-memory job registry disappears at restart, the
result remains uncertain and must not be automatically re-submitted.
If a provider reports a truncated body after process success, the result is
marked `incomplete` with `execution_status: succeeded`, `truncated: true`,
and `output_kind: partial`; it is never described as a final answer.
If execution had already been recorded as successful but its retained output
can no longer be read, `collab_result` returns `result_unavailable` with
`execution_status: succeeded`. It does not present a missing body as a result.

An adapter may also expose `status()`; calls are independently bounded in
parallel. Returned booleans are labelled as adapter reports, not as independent
evidence of execution, model quality, or billing. The accuracy of each adapter,
credential isolation, vendor policy, model identity, billing verification,
background job recovery, results and operating-system isolation have not been
established by these core tests. The standalone subprocess front end injects no adapters.
The MCP front end uses separate bounded in-process worker slots for each
provider and for status/check calls, so one provider reaching its limit does
not consume the other's slots or control-call slots. They are not durable workers; on
process exit an in-flight reservation remains uncertain and is not retried.
A durable background provider worker and recovery protocol are required before
integrating real providers. A local dispatch reservation is not proof that a
provider accepted a request. The front end returns server-busy when its bounded
worker capacity is exhausted, without processing that call.
Never treat a configured adapter, existing login, or passing unit tests as
proof of subscription-only billing. No uncertain request is auto-retried.

Run the offline checks from this directory:

```sh
python -m unittest -v test_coordinator test_mcp_stdio
```
