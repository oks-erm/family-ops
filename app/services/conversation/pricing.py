"""Dated standard API estimates, not billing. Unknown models cannot bypass dollar caps."""

from decimal import Decimal

PRICING_DATE = "2026-09-30"
# USD / million input, cached input, output. Cache writes add 25% of base input.
# https://developers.openai.com/api/docs/pricing
RATES = {
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.01"), Decimal("0.50")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("0.10"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("1"), Decimal("50")),
}


def estimate(model, inputs, outputs, *, cached=0, writes=0, reserve=False):
    rates = RATES.get(model)
    if rates is None:
        return None
    rate, cache_rate, output_rate = rates
    if inputs > 272000:
        rate, cache_rate, output_rate = rate * 2, cache_rate * 2, output_rate * Decimal("1.5")
    if reserve:
        writes = inputs
        cached = 0
    return (
        (
            (inputs - cached) * rate
            + cached * cache_rate
            + writes * rate * Decimal("0.25")
            + outputs * output_rate
        )
        / 1000000
    ).quantize(Decimal("0.00000001"))
