"""Shared absolute Opportunity Index; full precision components for reweighting."""
import math


def opportunity_score(return_score, protection_score, return_weight=30):
    if not 0 <= return_weight <= 100:
        raise ValueError('Return weight must be between 0 and 100')
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
               for x in (return_score, protection_score)):
        return 0.0
    w = return_weight / 100
    return round(1 / (w / return_score + (1-w) / protection_score), 1)


def score_candidate(candidate, weight=30):
    if 'return_score' in candidate and 'protection_score' in candidate:
        return opportunity_score(candidate['return_score'], candidate['protection_score'], weight)
    # Legacy candidate fixtures/records have no components. Never reweight them.
    return candidate.get('score', 0.) if weight == 30 else 0.
