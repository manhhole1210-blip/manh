"""Exact VND arithmetic. Fractional legacy balances require explicit reconciliation."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_VND = 9_000_000_000_000_000


def vnd(value):
    if isinstance(value, bool):
        raise ValueError('Invalid VND amount')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid VND amount') from None
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value() or amount > MAX_VND:
        raise ValueError('VND must be a nonnegative whole number within the supported range; reconcile legacy fractions first')
    return int(amount)


def crypto_to_vnd(amount_usd, exchange_rate):
    amount = Decimal(str(amount_usd))
    rate = Decimal(str(exchange_rate))
    if not amount.is_finite() or not rate.is_finite() or amount <= 0 or rate <= 0:
        raise ValueError('Invalid crypto amount or exchange rate')
    return vnd((amount * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
