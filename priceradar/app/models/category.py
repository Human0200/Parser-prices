from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

BigIntPK = BigInteger().with_variant(Integer, "sqlite")

from app.database import Base
from app.models.product import Marketplace


class TrackedCategory(Base):
    __tablename__ = "tracked_categories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "marketplace",
            "external_key",
            name="uq_user_marketplace_category",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marketplace: Mapped[Marketplace] = mapped_column(
        Enum(
            Marketplace,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    previous_min_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    min_price_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    min_price_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    parse_errors_count: Mapped[int] = mapped_column(Integer, default=0)
    last_parsed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="tracked_categories")  # noqa: F821
    snapshots: Mapped[list["CategorySnapshot"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )
    alert_rules: Mapped[list["AlertRule"]] = relationship(  # noqa: F821
        back_populates="category", cascade="all, delete-orphan"
    )


class CategorySnapshot(Base):
    __tablename__ = "category_snapshots"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tracked_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sku_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    median_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    category: Mapped["TrackedCategory"] = relationship(back_populates="snapshots")
