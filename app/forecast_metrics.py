"""Approximate inference for dated forecast errors, with calendar-lag HAC errors.

Tests are descriptive diagnostics, not a model approval or a causal price test.
"""
from datetime import date
import math
import statistics


FAMILY_SIZE = 24  # 3 measures × 4 horizons × (bias and paired MAE advantage)


def inference(points, lead):
    """Newey–West/Bartlett covariance of the mean, using actual calendar gaps.

    A normal approximation needs a substantial span. No p-value/CI is reported
    with <60 dates or <8 calendar blocks at lag max(7, horizon-1). Constant
    samples have no estimable sampling variance and get no inferential result.
    """
    points = sorted((date.fromisoformat(d), float(v)) for d, v in points)
    if len({d for d,_ in points}) != len(points):
        raise ValueError("Kun én observation pr. opholdsdato og horisont må indgå")
    n = len(points)
    mean = statistics.fmean(v for _, v in points) if n else None
    lag = max(7, lead - 1)
    blocks = len({d.toordinal() // (lag + 1) for d, _ in points})
    result = dict(mean=mean, n=n, lag=lag, blocks=blocks, p=None, ci=None, se=None,
                  reason='Kræver mindst 60 datoer og 8 kalenderblokke; ingen p-værdi endnu.')
    if n < 60 or blocks < 8:
        return result
    residuals = [(d, v-mean) for d, v in points]
    variance = sum(r*r for _, r in residuals)
    for i, (d, r) in enumerate(residuals):
        for other_day, other_r in residuals[i+1:]:
            gap = (other_day-d).days
            if gap > lag:
                break
            variance += 2 * (1-gap/(lag+1)) * r * other_r
    variance *= n/(n-1) / (n*n)
    if variance <= 1e-20:
        result['reason'] = 'Ingen målbar variation; p-værdi kan ikke estimeres.'
        return result
    se = math.sqrt(variance)
    result.update(se=se, p=math.erfc(abs(mean)/se/math.sqrt(2)),
                  ci=[mean-1.959963984540054*se, mean+1.959963984540054*se], reason='')
    return result


def summary(rows, lead):
    errors = [r['forecast']-r['actual'] for r in rows]
    paired = [r for r in rows if r.get('baseline') is not None]
    diffs = [(r['day'], abs(r['baseline']-r['actual'])-abs(r['forecast']-r['actual'])) for r in paired]
    bias = inference([(r['day'], r['forecast']-r['actual']) for r in rows], lead)
    advantage = inference(diffs, lead)
    eligible = [r for r in rows if '80' in r.get('intervals', {})]
    return dict(n=len(rows), n_baseline=len(paired),
        mae=statistics.fmean(abs(e) for e in errors) if errors else None,
        rmse=math.sqrt(statistics.fmean(e*e for e in errors)) if errors else None,
        baseline_mae=statistics.fmean(abs(r['baseline']-r['actual']) for r in paired) if paired else None,
        paired_model_mae=statistics.fmean(abs(r['forecast']-r['actual']) for r in paired) if paired else None,
        bias=bias, advantage=advantage,
        coverage80=sum(r['intervals']['80'][0]<=r['actual']<=r['intervals']['80'][1] for r in eligible)/len(eligible) if eligible else None)


def adjust(summaries):
    """Holm correction over the predeclared family, also reserving missing tests."""
    tests = [s[key] for s in summaries for key in ('bias','advantage') if s[key]['p'] is not None]
    ordered = sorted(tests, key=lambda t: t['p'])
    maximum = 0
    for i, test in enumerate(ordered):
        maximum = max(maximum, min(1, (FAMILY_SIZE-i)*test['p']))
        test['adjusted_p'] = maximum
    return summaries
