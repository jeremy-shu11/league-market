from __future__ import annotations

import math


PAYOUT_CREDITS = 100.0


def normalize_priors(priors: list[float] | None, outcome_count: int) -> list[float]:
    if outcome_count < 1:
        raise ValueError("At least one outcome is required")
    if priors is None:
        return [1.0 / outcome_count] * outcome_count
    if len(priors) != outcome_count:
        raise ValueError("Prior count must match outcome count")
    if any(not math.isfinite(value) or value <= 0 for value in priors):
        raise ValueError("Priors must be finite and positive")
    total = sum(priors)
    return [value / total for value in priors]


def lmsr_cost(
    quantities: list[float],
    liquidity: float,
    payout: float = PAYOUT_CREDITS,
    priors: list[float] | None = None,
) -> float:
    if liquidity <= 0:
        raise ValueError("Liquidity must be positive")
    if not quantities:
        raise ValueError("At least one outcome is required")

    normalized = normalize_priors(priors, len(quantities))
    scaled = [quantity / liquidity + math.log(prior) for quantity, prior in zip(quantities, normalized)]
    maximum = max(scaled)
    total = sum(math.exp(value - maximum) for value in scaled)
    return payout * liquidity * (maximum + math.log(total))


def lmsr_prices(
    quantities: list[float],
    liquidity: float,
    payout: float = PAYOUT_CREDITS,
    priors: list[float] | None = None,
) -> list[float]:
    if liquidity <= 0:
        raise ValueError("Liquidity must be positive")
    if not quantities:
        raise ValueError("At least one outcome is required")

    normalized = normalize_priors(priors, len(quantities))
    scaled = [quantity / liquidity + math.log(prior) for quantity, prior in zip(quantities, normalized)]
    maximum = max(scaled)
    weights = [math.exp(value - maximum) for value in scaled]
    total = sum(weights)
    return [payout * weight / total for weight in weights]


def trade_cost(
    quantities: list[float],
    outcome_index: int,
    shares_delta: float,
    liquidity: float,
    payout: float = PAYOUT_CREDITS,
    priors: list[float] | None = None,
) -> float:
    if outcome_index < 0 or outcome_index >= len(quantities):
        raise IndexError("Outcome index is out of range")
    if shares_delta == 0:
        return 0.0

    before = lmsr_cost(quantities, liquidity, payout, priors)
    next_quantities = list(quantities)
    next_quantities[outcome_index] += shares_delta
    after = lmsr_cost(next_quantities, liquidity, payout, priors)
    return after - before


def shares_for_budget(
    quantities: list[float],
    outcome_index: int,
    budget: float,
    liquidity: float,
    payout: float = PAYOUT_CREDITS,
    priors: list[float] | None = None,
    max_shares: float = 500.0,
) -> float:
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("Budget must be finite and positive")
    if not math.isfinite(max_shares) or max_shares <= 0:
        raise ValueError("Maximum shares must be finite and positive")
    maximum_cost = trade_cost(
        quantities, outcome_index, max_shares, liquidity, payout, priors
    )
    if budget >= maximum_cost:
        return max_shares
    low = 0.0
    high = max_shares
    for _ in range(64):
        midpoint = (low + high) / 2.0
        cost = trade_cost(quantities, outcome_index, midpoint, liquidity, payout, priors)
        if cost <= budget:
            low = midpoint
        else:
            high = midpoint
    return low


def worst_case_subsidy(priors: list[float], liquidity: float, payout: float = PAYOUT_CREDITS) -> float:
    normalized = normalize_priors(priors, len(priors))
    return payout * liquidity * max(math.log(1.0 / prior) for prior in normalized)


def shares_to_move_probability(
    quantities: list[float],
    outcome_index: int,
    liquidity: float,
    target_delta: float = 0.05,
    priors: list[float] | None = None,
) -> float:
    prices = lmsr_prices(quantities, liquidity, 1.0, priors)
    current = min(1.0 - 1e-9, max(1e-9, prices[outcome_index]))
    target = min(1.0 - 1e-9, current + target_delta)
    return max(0.0, liquidity * (math.log(target / (1.0 - target)) - math.log(current / (1.0 - current))))


def liquidity_value(label: str, outcome_count: int) -> float:
    normalized = (label or "medium").lower()
    if normalized == "low":
        return 20.0
    if normalized == "high":
        return 55.0
    if normalized == "medium-high":
        return 42.0
    return 35.0 if outcome_count <= 2 else 30.0
