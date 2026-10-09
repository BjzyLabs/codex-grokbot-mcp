# Security policy

Report vulnerabilities privately through the repository's GitHub security
reporting feature. Do not include credentials, prompts, callback bodies, or
deployment identifiers in public issues or pull requests.

Version 3 loads credentials from Vault at startup and stores only references
in an owner-only configuration file outside Git. Version 2 retains legacy
file-based credentials. There is no repository authority in this request path. A
worker answer is untrusted input even when it arrives on the expected callback
URL. The project fails closed when a required check or capability is
unavailable.
