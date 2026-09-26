# GitHub and webhook transport

The runtime reads one GitHub App secret from Vault at an operator-configured
path. The record contains `github_app_id`, `github_app_installation_id`, and
`github_app_private_key`. It signs a short-lived App JWT with the installed
OpenSSL command through a pipe; the PEM is never written to a file. The
installation-token request names exactly one configured control repository and
requests only `contents:write` and `pull_requests:write`. The response must
confirm that exact repository, those permissions, no additional authority
other than implicit metadata read, and at least 50 minutes remaining. A token
with an unexpected scope is revoked immediately.

The webhook URL and sender key also come from Vault. A job is sent once over
HTTPS. An HTTP error, timeout, or lost response is treated as uncertain: the
job manager must reconcile it before retrying or freeing the worker lease.
There is no fallback endpoint. The worker receives only the control-repository
token, never the App key, Vault credentials, or target-repository authority.

Coding jobs still return an open draft PR in the control repository. An
`x_query` callback does not use this transport. See
[result delivery](result-delivery.md). The coding transport
checks the exact branch and repository, then reads the artifact from the PR's
immutable head commit rather than a mutable branch name. The artifact remains
untrusted until the local path and patch validator checks its job identity,
snapshot binding, actual changed paths, and allowed write paths. Revocation of
the worker token is required after retrieval or a known terminal failure;
expiry covers a process crash.

Python must have a trusted CA bundle for both GitHub and the webhook. On hosts
whose Python installation has no default CA store, configure an explicit PEM
bundle for those transports or set the standard `SSL_CERT_FILE` environment
variable to a trusted system bundle. The Vault client can use its own CA
bundle. Certificate verification must remain enabled.
