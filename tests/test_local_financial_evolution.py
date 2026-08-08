from __future__ import annotations

import math

import pytest

from ralfloop_agent.local_arch.finance import FinancialConfig, evaluate_financial_strategy


def data():
    returns = [0.002 * math.sin(index / 4) + 0.0003 for index in range(120)]
    positions = [1.0 if index % 10 < 5 else -0.5 for index in range(120)]
    return returns, positions


def test_58_no_lookahead_position_shift():
    returns = [0.0] * 39 + [0.5]
    positions = [0.0] * 39 + [1.0]
    result = evaluate_financial_strategy(returns, positions, FinancialConfig(20, 30, survivorship_controlled=True))
    assert result.out_of_sample.cagr == 0


def test_59_no_leakage_disjoint_splits():
    returns, positions = data()
    result = evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True))
    assert result.train != result.validation != result.out_of_sample


@pytest.mark.parametrize("attribute", ["train", "validation", "out_of_sample"])
def test_60_62_explicit_splits(attribute):
    returns, positions = data()
    assert getattr(evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True)), attribute)


def test_63_walk_forward_windows():
    returns, positions = data()
    assert len(evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True)).walk_forward) >= 2


def test_64_fee_inclusion():
    returns, positions = data()
    zero = evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, fee_bps=0, survivorship_controlled=True))
    fee = evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, fee_bps=20, survivorship_controlled=True))
    assert fee.out_of_sample.transaction_costs > zero.out_of_sample.transaction_costs


def test_65_turnover_penalty():
    returns, positions = data()
    assert evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True)).turnover_penalty >= 0


@pytest.mark.parametrize("metric", ["max_drawdown", "sharpe", "stability"])
def test_66_68_risk_metrics(metric):
    returns, positions = data()
    result = evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True))
    assert math.isfinite(getattr(result.out_of_sample, metric))


def test_69_overfit_penalty_nonnegative():
    returns, positions = data()
    assert evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90, survivorship_controlled=True)).overfit_penalty >= 0


def test_70_deterministic_seed_equivalent_output():
    returns, positions = data()
    config = FinancialConfig(60, 90, survivorship_controlled=True)
    assert evaluate_financial_strategy(returns, positions, config) == evaluate_financial_strategy(returns, positions, config)


def test_survivorship_control_reported():
    returns, positions = data()
    assert evaluate_financial_strategy(returns, positions, FinancialConfig(60, 90)).reason == "SURVIVORSHIP_CONTROL_UNVERIFIED"
