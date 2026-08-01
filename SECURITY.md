# Security policy

Please report vulnerabilities privately through GitHub Security Advisories for
this repository. Do not open a public issue containing credentials, exploit
details, or incident data.

This project is a local reference stack, not an internet-facing service. Before
deployment, set `REMEDIATION_API_KEY`, restrict published ports and CORS origins,
use a managed secrets provider, replace SQLite/Redis LIST with production-grade
stores, add service-to-service identity and RBAC, and keep `SIMULATE_ONLY=true`
until runbooks have been reviewed.
