"""Turning what an editor typed into a price field into Square's integer minor units.

Pure Python (no SDK import). Prices reach the provider from CMS text fields as often as from
numbers, so "$1,170", "1170.50" and 250 must all work, and anything ambiguous must fail loudly
rather than list a painting at the wrong price.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS_PER_UNIT = 100
_CURRENCY_NOISE = re.compile(r"[\s$]")
# Commas only as thousands separators and at most two decimals, so a European "1.170,00" or a
# "$1.170" meant as 1,170 is rejected instead of silently listing the item at $1.17.
_TYPED_AMOUNT = re.compile(r"^(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?$|^\.\d{1,2}$")
_NUMERIC_AMOUNT = re.compile(r"^\d+(\.\d+)?$")  # str() of a non-negative, finite int or float


def to_cents(value: object, *, field: str = "price") -> int:
    """Parse a dollar amount (number, or a string like "$1,170.50") to integer cents.

    Raises ValueError for anything that is not a plain non-negative amount. Zero is returned as 0;
    callers decide whether zero is allowed.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be an amount, got {value!r}.")
    if isinstance(value, int | float):
        text, pattern = str(value), _NUMERIC_AMOUNT  # str() keeps 0.1 as "0.1", unlike Decimal(0.1)
    elif isinstance(value, str):
        text, pattern = _CURRENCY_NOISE.sub("", value), _TYPED_AMOUNT
    else:
        # ValueError, not TypeError: it is what Marvin core reports as a failed execution.
        raise ValueError(f"{field} must be a number or text like '$1,170', got {type(value).__name__}.")  # noqa: TRY004

    if not pattern.match(text):
        raise ValueError(f"{field} {value!r} is not an amount like '$1,170' or 1170.50.")
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation as e:  # pragma: no cover — the regex already rules this out
        raise ValueError(f"{field} {value!r} is not an amount.") from e
    return int((amount * CENTS_PER_UNIT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def price_cents(value: object) -> int:
    """A sale price: required and strictly positive."""
    cents = to_cents(value, field="price")
    if cents <= 0:
        raise ValueError(f"price must be greater than zero, got {value!r}.")
    return cents


def optional_fee_cents(value: object, *, field: str = "shipping_fee") -> int:
    """An optional fee: blank or missing means no fee (0); otherwise a non-negative amount."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    return to_cents(value, field=field)
