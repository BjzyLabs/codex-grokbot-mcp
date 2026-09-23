# Vault setup contract

Vault is required for credentials and shared worker leasing. The runtime uses AppRole credentials in two local, regular files with owner-only permissions. It authenticates for each Vault operation and never stores a Vault token in durable job state. Vault TLS verification remains enabled.

The operator chooses a KV v2 mount, credential paths, and one lease path per worker. A dedicated AppRole should have only these capabilities:

```hcl
path "secret/data/grokbot/coder-webhook" {
  capabilities = ["read"]
}

path "secret/data/grokbot/github-app" {
  capabilities = ["read"]
}

path "secret/metadata/grokbot/leases/coder" {
  capabilities = ["read"]
}

path "secret/data/grokbot/leases/coder" {
  capabilities = ["create", "read", "update"]
}
```

The paths above are synthetic examples. Put the exact deployment policy and AppRole setup in a private, source-controlled operations repository. Do not commit role IDs, SecretIDs, tokens, private keys, webhook keys, or deployment paths here.

Before dispatch, the lease key's KV v2 metadata must exist with `cas_required=true`. The runtime verifies that flag. CAS 0 creates an absent lease record; every later write uses the observed version. An active lease, even one past its heartbeat expiry, is never taken over automatically. Each job has a 45-minute deadline; the healthy watcher renews the five-minute heartbeat within that limit. An ambiguous write remains uncertain until a read proves its state. Release is permitted only after the job manager has verified a terminal outcome.

The existing dispatcher must use the same lease key and record schema before either dispatcher sends a live job to the shared worker. Live AppRole access, metadata configuration, and cross-dispatcher behavior remain separate acceptance gates.
