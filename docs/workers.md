# Workers and task size

Each configured Grok Bot worker has an explicit worker ID, webhook credential
path, GitHub App credential path, and Vault KV v2 CAS lease key. Adding a worker
requires those exact resources and a tested route. A busy or failed worker does
not trigger automatic routing to another Bot. Every dispatcher that can reach
the same Bot must claim the same lease key before dispatch.

Cursor [documents concurrent Bots](https://cursor.com/docs/grok-bot), but Bots
under one account share a cloud computer, files, browser sessions, and app
logins. A per-Bot lease prevents overlapping jobs for that Bot; it does not
isolate one Bot's data from another on the shared computer. Operators must
approve every Bot's access to delegated source and keep control-repository
branches and artifacts unique to the job. Worker count is not a security
boundary.

A delegated job should state its goal, the exact source snapshots, allowed
write paths, acceptance checks, and the 45-minute maximum. Larger work should
be split into bounded jobs before submission. A task-size or effort estimate
may help the Bot plan its response, but it is advisory and never relaxes path,
lease, deadline, or review checks.

Grok Bot's [settings documentation](https://cursor.com/docs/grok-bot/settings)
says Cursor manages its model selection and provides no model picker. This
integration therefore makes no per-job model promise. Cursor Cloud Agents and
Automations have separate model controls; those APIs are outside this Grok Bot
webhook contract. A future model-aware route would require documented Grok Bot
support and a revised plan.
