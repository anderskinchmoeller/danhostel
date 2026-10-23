"""Finite scenario decisions, not confidence bounds or causal revenue estimates.

Inspired by minimax regret in Bian, Wang & Qi (arXiv:2411.08126v3).
This is a one-date, constant-elasticity adaptation, not their fitted demand
bounds, sequential algorithm, or statistical guarantee.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Action:
    key: tuple
    values: tuple[float, ...]


def minimax_regret(actions, nominal_index=0, *, oracle=None):
    """Choose one implementable action for all scenarios, including inventory.

    The oracle may choose differently in each scenario. Our action cannot.
    Scenarios have no assigned probabilities. Ties prefer nominal contribution,
    then the supplied order. Returns the action and its largest opportunity loss.
    """
    if not actions or not actions[0].values:
        raise ValueError("Mindst én handling og ét scenarie kræves")
    size = len(actions[0].values)
    if not 0 <= nominal_index < size:
        raise ValueError("Ugyldigt nominelt scenarie")
    if any(len(a.values) != size or any(not math.isfinite(v) for v in a.values)
           for a in actions):
        raise ValueError("Scenarier skal have samme længde og endelige værdier")
    oracle = tuple(max(a.values[i] for a in actions) for i in range(size)) if oracle is None else tuple(oracle)
    if len(oracle) != size or any(not math.isfinite(v) for v in oracle):
        raise ValueError("Sammenligningsgrundlaget skal matche scenarierne")
    regret = lambda a: max(0.0, max(o-v for o, v in zip(oracle, a.values)))
    best = min(actions, key=lambda a: (round(regret(a), 8), -a.values[nominal_index]))
    return best, regret(best)
