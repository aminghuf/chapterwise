"""Alembic environment. Run migrations with `chapterwise migrate`."""

from alembic import context
from sqlalchemy import create_engine

from chapterwise.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    # The URL is passed through attributes, not the ini file, so passwords with % don't break parsing.
    engine = create_engine(config.attributes["database_url"])
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
