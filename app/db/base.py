from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from config import (
    SQLALCHEMY_DATABASE_URL,
    SQLALCHEMY_POOL_RECYCLE,
    SQLALCHEMY_POOL_SIZE,
    SQLIALCHEMY_MAX_OVERFLOW,
)

IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith('sqlite')

# SQLSTATE 25006 -- read_only_sql_transaction.
# Raised by a Postgres standby when it receives a write. In a Patroni cluster
# this is how a panel finds out the node it is pooled against was demoted.
READ_ONLY_SQLSTATE = "25006"

if IS_SQLITE:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_size=SQLALCHEMY_POOL_SIZE,
        max_overflow=SQLIALCHEMY_MAX_OVERFLOW,
        pool_recycle=SQLALCHEMY_POOL_RECYCLE,
        pool_timeout=10,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "handle_error")
    def _invalidate_connection_on_read_only(context):
        """Drop connections to a demoted Postgres primary.

        ``pool_pre_ping`` only issues ``SELECT 1``, which succeeds against a
        read-only standby, so it cannot detect that Patroni moved the primary
        elsewhere. Flagging the error as a disconnect makes SQLAlchemy discard
        the whole pool; the next connect goes through libpq's multi-host
        ``target_session_attrs=read-write`` resolution and lands on the new
        primary.
        """
        sqlstate = getattr(context.original_exception, "sqlstate", None)
        if sqlstate is None:
            # psycopg2 exposes it under a different name than psycopg3.
            sqlstate = getattr(context.original_exception, "pgcode", None)
        if sqlstate == READ_ONLY_SQLSTATE:
            context.is_disconnect = True


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass
