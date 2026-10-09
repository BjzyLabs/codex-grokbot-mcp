# Contributing

This repository and its complete Git history are public. Use synthetic `.example` and `.invalid` values in source, tests, Beads records, and documentation. Never commit private endpoints, hostnames, webhook URLs, tokens, secrets, or local deployment configuration.

Use [Beads 1.3+ with Dolt](docs/beads.md) for bounded tasks and dependencies.
Keep `.beads/` runtime state out of Git and review task history before `bd dolt push`. For every behavior change, demonstrate red, green, and refactor with a meaningful test. Do not bypass a failing check or substitute credentials, transports, hosts, models, mocks, or weaker validation to make a live canary pass. Record prerequisite or capability failures in Beads and stop dependent work until a revised plan is approved.

Start feature work from `develop` on a task branch. Open tested PRs to `develop`. Promote `develop` to `main` only after milestone acceptance. Keep commits conventional, such as `feat(protocol): validate patch paths`.

Before pushing, review `git diff --cached`, `git ls-files`, and Git history; run tests, lint, Gitleaks, and the local private denylist check documented in the README. The private denylist is kept outside this repository. Never use `git add --force` to include ignored private context.
