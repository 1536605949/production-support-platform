from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings

class Base(DeclarativeBase):
    pass

s = get_settings()
if s.database_url.startswith('sqlite:///') and not s.database_url.endswith(':memory:'):
    from pathlib import Path
    raw = s.database_url.removeprefix('sqlite:///')
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
connect_args = {'check_same_thread': False} if s.database_url.startswith('sqlite') else {}
engine = create_engine(s.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
