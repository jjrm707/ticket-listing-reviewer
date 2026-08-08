from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def _require_decimals(*values: Decimal) -> None:
    if not all(isinstance(value, Decimal) for value in values):
        raise ValueError("pricing inputs must be Decimal values")


def money(value: Decimal) -> Decimal:
    _require_decimals(value)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def acquisition_total(
    pair_price: Decimal, buyer_fees: Decimal, estimated_tax: Decimal
) -> Decimal:
    _require_decimals(pair_price, buyer_fees, estimated_tax)
    return money(pair_price + buyer_fees + estimated_tax)


def projected_proceeds(resale_gross: Decimal, seller_fee_rate: Decimal) -> Decimal:
    _require_decimals(resale_gross, seller_fee_rate)
    return money(resale_gross * (Decimal("1") - seller_fee_rate))


def net_profit(proceeds: Decimal, acquisition: Decimal) -> Decimal:
    _require_decimals(proceeds, acquisition)
    return money(proceeds - acquisition)


def roi(profit: Decimal, acquisition: Decimal) -> Decimal | None:
    _require_decimals(profit, acquisition)
    if acquisition == 0:
        return None
    return (profit / acquisition).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
