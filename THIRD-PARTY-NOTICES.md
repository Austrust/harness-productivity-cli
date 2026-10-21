# Third-party dependencies

This repository contains the Python CLI source, tests, examples, and a harness-independent Skill. It does not vendor dependency libraries, Python runtimes, or native executables.

Runtime dependencies are declared in `pyproject.toml` and installed by pip under their upstream licenses. In particular, `pyicloud==2.6.5` provides Apple sessions, CloudKit records, and Reminders protocol/CRDT helpers; `requests` provides HTTP transport, `icalendar` handles calendar serialization, `msal` handles Microsoft delegated login, and `keyring`/`SecretStorage` provide OS credential storage. `tzdata` provides timezone data when the OS does not.

See each installed dependency's package metadata and license files for its applicable license. The MIT license in this repository covers this project's own code and Skill; it does not replace upstream licenses.

Upstream sources:

- pyicloud: https://github.com/picklepete/pyicloud
- requests: https://github.com/psf/requests
- icalendar: https://github.com/collective/icalendar
- MSAL Python: https://github.com/AzureAD/microsoft-authentication-library-for-python
- keyring: https://github.com/jaraco/keyring
- SecretStorage: https://github.com/mitya57/secretstorage
- tzdata: https://github.com/python/tzdata
