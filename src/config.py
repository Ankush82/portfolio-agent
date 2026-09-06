"""Application configuration — module-level constants with type annotations.

Follows the pattern of dataclass-style config values. Import directly:
    from config import AUDIT_TABLE_NAME, AUDIT_MAX_QUERY_LIMIT, ...

All values here are intentionally hardcoded defaults (no env-var override
for now). They are the single source of truth for application-wide
behaviour that needs to be consistent across multiple modules.
"""

# ---------------------------------------------------------------------------
# Audit / event-observation settings
# ---------------------------------------------------------------------------

# Name of the table (or log file) where audit events are stored.
AUDIT_TABLE_NAME: str = "audit_events"

# Hard cap on the number of rows a single audit query may return.
AUDIT_MAX_QUERY_LIMIT: int = 1000

# Default page-size when no explicit limit is passed to AuditReader.query().
AUDIT_DEFAULT_LIMIT: int = 100

# How long audit events are retained before being eligible for cleanup (days).
AUDIT_RETENTION_DAYS: int = 365
