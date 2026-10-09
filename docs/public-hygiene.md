# Public identifier policy and historical exceptions

The public repository must use synthetic fixture values and must not introduce
private endpoints, credentials, deployment configuration, or Vault paths.
The external private denylist remains unchanged. Gitleaks is a separate gate
and has no exceptions for these findings.

## Approved legacy metadata exception

On 2026-10-09 the owner accepted a Vault mount name already present in old
public test fixtures and approved exact historical exceptions instead of a
history rewrite. The finding is metadata, not a credential. Current files
already use synthetic values; no private runtime configuration is published.

The exception covers only these three historical blob versions:

| Historical file | Git blob ID |
| --- | --- |
| `tests/test_coordinator.py` | `f75edeff80a3bb754b10971fc6a114581638fe1e` |
| `tests/test_diagnostic_coordinator.py` | `aa7f52d8523e974ff8896f953f57d18116f10a86` |
| `tests/test_diagnostic_coordinator.py` | `3ac257b843f439ec04fb1df15e10d9d55582e406` |

`scripts/check_public_hygiene.py` binds each exception to the exact file path,
SHA-256 content fingerprint, and SHA-256 fingerprint of the single accepted
denylist identifier. The identifier is not copied into this policy or the
scanner. All other denied identifiers within those blobs still fail.

The staged scan never accepts exceptions, including when an identical old
fixture is reintroduced. Changed content and other file paths do not qualify.
Filename matches always fail. Further exceptions require explicit owner review;
do not expand these entries or remove terms from the external denylist.

## Validation

Run the normal gates from the project virtual environment:

```sh
python -m pytest tests/test_public_hygiene.py -q
python scripts/check_public_hygiene.py --denylist /absolute/private/denylist.txt
gitleaks git --redact --log-opts=--all
gitleaks git --staged --redact
```

Tests prove that an exact approved historical match passes, while staged
reintroductions, changed historical content, a different file path, and another
denied identifier in an approved blob fail without displaying private values.
