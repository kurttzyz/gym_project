# Tenant infrastructure

Customer gyms are registry records and PostgreSQL databases, not Python folders.
Do not add `gym_a/`, `gym_b/`, etc. with copied code or credentials here.
`middleware.py` resolves verified custom domains to their registry organization;
all gym portals use the same installed applications and templates.

See ../docs/modules.md and ../docs/custom-domains.md.
