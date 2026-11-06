"""Alembic environment.

The application passes its own connection (``Database.upgrade``); the ``alembic`` command line
(``uv run alembic revision --autogenerate -m "..."``) connects with the application settings.
"""

from alembic import context

from agaura.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    # Batch mode rewrites tables for ALTER operations SQLite does not support.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    from agaura.config import get_settings
    from agaura.db import Database, migration_transaction

    database = Database(get_settings().sqlalchemy_url)
    with database.engine.connect() as own_connection, migration_transaction(own_connection):
        run_migrations(own_connection)
    database.dispose()
