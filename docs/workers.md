# The single configured Bot

This requestor talks to exactly one Grok Bot: the Chief of Staff behind the
configured webhook. That identity is fixed in the configuration by its webhook
URL and sender key; there is no worker table, no worker selection, and no
fallback to another Bot. Cancelling a different account or Bot does not affect
this path.

Two request types are supported, both read-only:

- `x_query` for public X research, where access to X is the reason to ask;
- `ask` for any other bounded question with the same quick turnaround.

Because both are read-only, requests may overlap. The Bot serialises them in
its own conversation; the requestor does not need mutual exclusion, and a
second question cannot corrupt the first because each answer arrives on its own
per-job callback path.

There is nothing to size, lease, or queue locally. A question should still be
bounded: it must fit 2000 characters, and the job has 45 minutes to answer
before the requestor marks it `uncertain`.

Planned repository missions (review-only work across the GitHub organisation)
are a later phase and are not part of this contract. They would need their own
authority model, because flagging issues and reviewing code are not read-only
answers.
