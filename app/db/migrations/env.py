from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.db.base import Base
import app.db.models  # noqa: F401
from config import SQLALCHEMY_DATABASE_URL

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
config.set_main_option('sqlalchemy.url', SQLALCHEMY_DATABASE_URL)

# Arbitrary but fixed; any panel running Marzban migrations uses the same key.
MIGRATION_LOCK_ID = 8474219003115

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # With two panels, both containers may run `alembic upgrade head` at
        # boot and collide on CREATE TABLE and on the alembic_version update.
        # The lock is taken outside begin_transaction() so the loser waits on
        # the lock rather than deadlocking inside a transaction.
        #
        # An advisory lock is the right tool *here* -- short-lived, on a
        # dedicated connection (poolclass=NullPool guarantees one connection
        # for the whole run) and released if the process dies. That is the
        # opposite of the leader lease, where a long-lived pooled engine makes
        # advisory locks the wrong choice.
        locked = connection.dialect.name == "postgresql"
        if locked:
            connection.exec_driver_sql(
                "SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,)
            )
        try:
            context.configure(
                connection=connection, target_metadata=target_metadata
            )

            with context.begin_transaction():
                context.run_migrations()
        finally:
            if locked:
                connection.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,)
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
