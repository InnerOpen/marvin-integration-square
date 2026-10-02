"""Tests for price parsing — pure Python, no SDK."""

import pytest

from marvin_integration_square.money import optional_fee_cents, price_cents


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("$1,170", 117000),
        (250, 25000),
        (250.5, 25050),
        ("1170.50", 117050),
        (" $ 12.3 ", 1230),
        (12.345, 1235),  # numbers round half up to the cent
        ("0.99", 99),
    ],
)
def test_price_cents_with_valid_amount_returns_cents(raw, cents):
    assert price_cents(raw) == cents


@pytest.mark.parametrize(
    "raw", ["abc", 0, "0", "$0.00", -5, "-5", "", "1.170,00", "$1.170", "1,17", "12,3456", "12e3", None, True, float("nan"), [1]]
)
def test_price_cents_with_invalid_or_non_positive_amount_raises(raw):
    with pytest.raises(ValueError):
        price_cents(raw)


@pytest.mark.parametrize(("raw", "cents"), [(None, 0), ("", 0), ("  ", 0), (0, 0), ("$25", 2500), (12.5, 1250)])
def test_optional_fee_cents_treats_blank_as_zero(raw, cents):
    assert optional_fee_cents(raw) == cents


def test_optional_fee_cents_with_garbage_raises():
    with pytest.raises(ValueError, match="shipping_fee"):
        optional_fee_cents("free")
