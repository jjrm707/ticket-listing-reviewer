from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def acquisition_total(
    pair_price: Decimal, buyer_fees: Decimal, estimated_tax: Decimal
) -> Decimal:
    return money(pair_price + buyer_fees + estimated_tax)


def projected_proceeds(resale_gross: Decimal, seller_fee_rate: Decimal) -> Decimal:
    return money(resale_gross * (Decimal("1") - seller_fee_rate))


def net_profit(proceeds: Decimal, acquisition: Decimal) -> Decimal:
    return money(proceeds - acquisition)


def roi(profit: Decimal, acquisition: Decimal) -> Decimal | None:
    if acquisition == 0:
        return None
    return (profit / acquisition).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
