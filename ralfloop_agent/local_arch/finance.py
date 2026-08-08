from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import statistics
from typing import Iterable, Sequence


@dataclass(frozen=True)
class FinancialConfig:
    train_end: int
    validation_end: int
    fee_bps: float = 10.0
    periods_per_year: int = 252
    complexity: int = 1
    survivorship_controlled: bool = False


@dataclass(frozen=True)
class FinancialMetrics:
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    turnover: float
    transaction_costs: float
    exposure: float
    volatility: float
    win_loss: float
    tail_risk: float
    stability: float


@dataclass(frozen=True)
class FinancialEvaluation:
    valid: bool
    reason: str
    train: FinancialMetrics
    validation: FinancialMetrics
    out_of_sample: FinancialMetrics
    walk_forward: tuple[FinancialMetrics, ...]
    overfit_penalty: float
    turnover_penalty: float
    complexity_penalty: float
    instability_penalty: float
    score: float | None


def evaluate_financial_strategy(
    asset_returns: Sequence[float],
    positions: Sequence[float],
    config: FinancialConfig,
) -> FinancialEvaluation:
    """Objective evaluator. Position at t is applied only to return t+1."""
    if len(asset_returns) != len(positions) or len(asset_returns) < 30:
        raise ValueError("financial_series_length")
    if not 5 <= config.train_end < config.validation_end <= len(asset_returns) - 5:
        raise ValueError("invalid_financial_split")
    if config.fee_bps < 0:
        raise ValueError("invalid_fee")
    if any(not math.isfinite(value) or abs(value) > 1.0 for value in positions):
        raise ValueError("invalid_exposure")
    fees = config.fee_bps / 10_000
    realized: list[float] = [0.0]
    for index in range(1, len(asset_returns)):
        turnover = abs(positions[index - 1] - positions[index - 2]) if index > 1 else abs(positions[0])
        realized.append(positions[index - 1] * asset_returns[index] - turnover * fees)
    train = _metrics(realized[: config.train_end], positions[: config.train_end], config)
    validation = _metrics(realized[config.train_end : config.validation_end], positions[config.train_end : config.validation_end], config)
    out = _metrics(realized[config.validation_end :], positions[config.validation_end :], config)
    walk = _walk_forward(realized, positions, config)
    overfit = max(0.0, train.sharpe - out.sharpe) * 0.15
    turnover_penalty = max(0.0, out.turnover - 0.25) * 0.10
    complexity_penalty = math.log1p(max(0, config.complexity - 1)) * 0.02
    stability_values = [item.sharpe for item in walk]
    instability = statistics.pstdev(stability_values) * 0.10 if len(stability_values) > 1 else 0.0
    valid = all(math.isfinite(item) for item in (train.sharpe, validation.sharpe, out.sharpe))
    reason = "OK" if valid else "NON_FINITE_METRICS"
    if not config.survivorship_controlled:
        reason = "SURVIVORSHIP_CONTROL_UNVERIFIED"
    score = None
    if valid:
        score = 0.25 * validation.sharpe + 0.45 * out.sharpe + 0.15 * out.sortino - 0.15 * abs(out.max_drawdown)
        score -= overfit + turnover_penalty + complexity_penalty + instability
    return FinancialEvaluation(valid, reason, train, validation, out, walk, overfit, turnover_penalty, complexity_penalty, instability, score)


def _metrics(returns: Sequence[float], positions: Sequence[float], config: FinancialConfig) -> FinancialMetrics:
    count = max(1, len(returns))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    wins = 0
    losses = 0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        wins += value > 0
        losses += value < 0
    mean = statistics.fmean(returns) if returns else 0.0
    volatility = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    downside = [min(0.0, value) for value in returns]
    downside_dev = statistics.pstdev(downside) if len(downside) > 1 else 0.0
    annualizer = math.sqrt(config.periods_per_year)
    sharpe = mean / volatility * annualizer if volatility else 0.0
    sortino = mean / downside_dev * annualizer if downside_dev else 0.0
    cagr = equity ** (config.periods_per_year / count) - 1 if equity > 0 else -1.0
    turnover = sum(abs(positions[index] - positions[index - 1]) for index in range(1, len(positions))) / count
    transaction_costs = turnover * count * config.fee_bps / 10_000
    exposure = statistics.fmean(abs(value) for value in positions) if positions else 0.0
    ordered = sorted(returns)
    tail_count = max(1, int(len(ordered) * 0.05))
    tail = statistics.fmean(ordered[:tail_count]) if ordered else 0.0
    stability = 1 / (1 + volatility + turnover)
    return FinancialMetrics(cagr, max_drawdown, sharpe, sortino, turnover, transaction_costs, exposure, volatility * annualizer, wins / max(1, losses), tail, stability)


def _walk_forward(returns: Sequence[float], positions: Sequence[float], config: FinancialConfig) -> tuple[FinancialMetrics, ...]:
    start = config.train_end
    span = max(5, (len(returns) - start) // 4)
    windows: list[FinancialMetrics] = []
    while start < len(returns):
        end = min(len(returns), start + span)
        windows.append(_metrics(returns[start:end], positions[start:end], config))
        start = end
    return tuple(windows)
