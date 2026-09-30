# Phase I request path

This file records the design the repository now implements. It replaces the
earlier delivery plan, which described code delegation.

**Decision.** One Grok Bot, two read-only request types, webhook in and
callback out. The requestor keeps no shared lock and holds no repository
authority.

**What changed.** The shared lease, the control repository, the GitHub App
installation, the Vault client, the coding packet, the artifact validator, the
worker-diagnostic route, the workspace opt-in, and the second account are all
gone from this repository, along with their modules, tests, and documents. The
v3 envelope, the callback inbox, and the result validator remain.

**Why it is safe without a lease.** A lease never proved the Bot was idle; a
webhook 200 only means the request was accepted. What actually prevents
cross-talk is the job ID plus one stored body per job. That contract is
enforced by the inbox and verified by the coordinator, and it holds under
concurrent dispatchers.

**Phase II.** Repository missions across the organisation — reviewing new
code, flagging problems, and summarising findings — are expected to reuse the
same webhook and inbox rail, but they need their own authority model, because
reviewing code is not a read-only answer. Those decisions are deferred and are
not part of Phase I.
