"""SQLite history and publishing queue."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable

from sqlalchemy import DateTime, Integer, String, Text, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from config.settings import DATABASE_PATH


class Base(DeclarativeBase):
    pass


class PostRecord(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    media_json: Mapped[str] = mapped_column(Text, default="[]")
    platforms_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    result_json: Mapped[str] = mapped_column(Text, default="{}")

    @property
    def media_paths(self) -> list[str]:
        return json.loads(self.media_json)

    @property
    def platforms(self) -> list[str]:
        return json.loads(self.platforms_json)

    @property
    def results(self) -> dict[str, dict[str, object]]:
        return json.loads(self.result_json)


class PostRepository:
    def __init__(self) -> None:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{DATABASE_PATH.as_posix()}")
        Base.metadata.create_all(self.engine)

    def create(
        self,
        *,
        text: str,
        media_paths: Iterable[str],
        platforms: Iterable[str],
        status: str,
        scheduled_at: datetime | None = None,
        results: dict[str, dict[str, object]] | None = None,
    ) -> int:
        record = PostRecord(
            text=text,
            media_json=json.dumps(list(media_paths), ensure_ascii=False),
            platforms_json=json.dumps(list(platforms), ensure_ascii=False),
            status=status,
            scheduled_at=scheduled_at,
            result_json=json.dumps(results or {}, ensure_ascii=False),
        )
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def update_result(self, post_id: int, status: str, results: dict[str, dict[str, object]]) -> None:
        with Session(self.engine, expire_on_commit=False) as session:
            record = session.get(PostRecord, post_id)
            if record is None:
                return
            record.status = status
            record.result_json = json.dumps(results, ensure_ascii=False)
            session.commit()

    def recent(self, limit: int = 100) -> list[PostRecord]:
        with Session(self.engine, expire_on_commit=False) as session:
            query = select(PostRecord).order_by(PostRecord.created_at.desc()).limit(limit)
            return list(session.scalars(query))

    def due(self, now: datetime) -> list[PostRecord]:
        with Session(self.engine, expire_on_commit=False) as session:
            query = select(PostRecord).where(
                PostRecord.status == "queued", PostRecord.scheduled_at <= now
            ).order_by(PostRecord.scheduled_at).limit(1)
            records = list(session.scalars(query))
            for record in records:
                record.status = "publishing"
            session.commit()
            for record in records:
                session.expunge(record)
            return records

    def queued(self) -> list[PostRecord]:
        with Session(self.engine, expire_on_commit=False) as session:
            query = select(PostRecord).where(PostRecord.status == "queued").order_by(PostRecord.scheduled_at)
            return list(session.scalars(query))

    def delete(self, post_id: int) -> bool:
        with Session(self.engine) as session:
            result = session.execute(delete(PostRecord).where(PostRecord.id == post_id))
            session.commit()
            return bool(result.rowcount)
