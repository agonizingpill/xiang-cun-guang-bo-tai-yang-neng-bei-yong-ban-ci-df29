from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine

from core.config import DATABASE_URL

metadata = MetaData()
_connect_args = {'timeout': 30} if DATABASE_URL.startswith('sqlite') else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)

@contextmanager
def transaction():
    with engine.begin() as conn:
        yield conn
