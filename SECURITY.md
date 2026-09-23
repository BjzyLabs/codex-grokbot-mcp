# Security policy

Report vulnerabilities privately through the repository's GitHub security reporting feature. Do not include credentials, private source, worker artifacts, or deployment identifiers in public issues or pull requests.

The supported credential and lease backend is HashiCorp Vault. Runtime configuration belongs outside Git. A worker result is untrusted input even when it comes from the configured control repository. The project fails closed when a required check or capability is unavailable.
