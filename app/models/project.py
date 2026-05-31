from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None]
    enable_technical_refinement: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    plan_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    last_planned_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    runtime_spec: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )
    product_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
