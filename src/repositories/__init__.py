"""Typed repositories over the Infrastructure Protocol.

A repository owns the row-mapping and CRUD logic for one domain
entity, talking to storage exclusively through the injected
``Infrastructure`` Protocol — never through a concrete backend
(``DefaultInfrastructure``, psycopg, redis, sqlalchemy, ...) and
never by emitting SQL. The Protocol is the only contract.

The first repository in this package is :class:`UserRepository`
(STORY-5). New repositories will be added here as later stories
land.

Repositories are stateless apart from the injected infrastructure:
no module-level caches, no connection handling, no use of
``cache_get``/``cache_set``. All persistence goes through
``infrastructure.store``/``retrieve``/``query``/``delete``.
"""

from repositories.user_repository import UserRepository

__all__ = ["UserRepository"]
