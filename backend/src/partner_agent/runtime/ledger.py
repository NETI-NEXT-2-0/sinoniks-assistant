"""Transactional daily usage reservation for model calls."""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.infra.models import ModelUsageDaily
from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import ProviderError


def micros(value: float) -> int:
    return int((Decimal(str(value)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def reserve_daily_usage(
    db: AsyncSession, settings: Settings, estimate_usd: float
) -> datetime | None:
    if settings.runtime_profile == "offline_fixture" or not settings.model_daily_cap_usd:
        return None
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = await db.get(ModelUsageDaily, (day, settings.runtime_profile, settings.model_name),
        with_for_update=True)
    if row is None:
        row = ModelUsageDaily(usage_day=day, profile=settings.runtime_profile,
            model=settings.model_name)
        db.add(row)
        await db.flush()
    amount = micros(estimate_usd)
    if row.spent_usd_micros + amount > micros(settings.model_daily_cap_usd):
        raise ProviderError("provider_daily_cap_exceeded")
    row.spent_usd_micros += amount
    row.request_count += 1
    await db.flush()
    return day


async def reconcile_daily_usage(
    db: AsyncSession, settings: Settings, day: datetime | None,
    reserved_usd: float, actual_usd: float,
) -> None:
    if day is None:
        return
    row = await db.get(
        ModelUsageDaily, (day, settings.runtime_profile, settings.model_name), with_for_update=True
    )
    if row is None:
        raise ProviderError("provider_ledger_missing")
    correction = micros(reserved_usd) - micros(actual_usd)
    row.spent_usd_micros = max(0, row.spent_usd_micros - correction)
    await db.flush()
