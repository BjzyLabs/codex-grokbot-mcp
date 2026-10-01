# Security policy

Report vulnerabilities privately through the repository's GitHub security
reporting feature. Do not include credentials, prompts, callback bodies, or
deployment identifiers in public issues or pull requests.

The only credential store is the owner-only configuration file outside Git.
There is no secret backend and no repository authority in this request path. A
worker answer is untrusted input even when it arrives on the expected callback
URL. The project fails closed when a required check or capability is
unavailable.
