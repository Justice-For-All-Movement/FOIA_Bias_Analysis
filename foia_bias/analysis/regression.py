"""FBA-12 regressions, diagnostics and honest estimands (standard library only).

Models over FBA-11 cohort rows:

* wrongdoing: logit of ``wrongdoing_any`` on ``same_party`` (+ fixed effects)
* favorability: OLS of ``fav_target`` on ``same_party`` (+ fixed effects);
  positive ``same_party`` means documents about the incumbent's party are more
  favorable to that party than documents about the opposition (incumbent-aligned)

Fixed effects are dummy-coded with the alphabetically first level as
reference. Covariance is classical or cluster-robust (CR1) by a named column.
Confidence intervals use the normal approximation (z = 1.959964).

Estimands are associations among **observed disclosures**. They do not
identify unreleased content, suppression, release probabilities or causal
effects. Unsuitable data returns ``status: "diagnostic"`` with a reason and no
coefficient instead of a false estimate: empty input, no outcome/regressor
variation, singular designs, separation/non-convergence and too few clusters.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass

from foia_bias.ledger import canonical

Z95 = 1.959963984540054
ESTIMAND_NOTE = ("Association among observed disclosed documents in the cohort; not evidence of suppression, "
                 "release probability or causation.")


@dataclass(frozen=True)
class ModelSpec:
    model: str  # "logit" | "ols"
    outcome: str
    regressor: str = "same_party"
    fixed_effects: tuple = ("agency", "year")
    cluster: str | None = "agency"
    min_clusters: int = 10
    min_rows: int = 20

    def __post_init__(self):
        if self.model not in ("logit", "ols"):
            raise ValueError("model must be logit or ols")

    def describe(self):
        formula = f"{self.outcome} ~ {self.regressor}" + "".join(f" + C({fe})" for fe in self.fixed_effects)
        out = asdict(self)
        out.update(formula=formula, covariance=f"cluster:{self.cluster}" if self.cluster else "classical")
        out["spec_sha256"] = hashlib.sha256(canonical(out).encode()).hexdigest()
        return out


def wrongdoing_spec(**kw):
    return ModelSpec("logit", "wrongdoing_any", **kw)


def favorability_spec(**kw):
    return ModelSpec("ols", "fav_target", **kw)


# -- linear algebra ---------------------------------------------------------
def _solve(matrix, vectors):
    """Gauss-Jordan with partial pivoting. Returns (inverse-applied columns, rank_ok)."""
    n = len(matrix)
    a = [list(map(float, row)) + list(map(float, vec)) for row, vec in zip(matrix, vectors)]
    scale = max((abs(x) for row in matrix for x in row), default=1.0) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-10 * scale:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        inv = 1.0 / a[col][col]
        a[col] = [x * inv for x in a[col]]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                factor = a[r][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col])]
    return [row[n:] for row in a]


def _inverse(matrix):
    n = len(matrix)
    identity = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    return _solve(matrix, identity)


def _xtwx(X, w):
    k = len(X[0])
    out = [[0.0] * k for _ in range(k)]
    for row, weight in zip(X, w):
        for i in range(k):
            ri = row[i] * weight
            if ri:
                for j in range(i, k):
                    out[i][j] += ri * row[j]
    for i in range(k):
        for j in range(i):
            out[i][j] = out[j][i]
    return out


def _matvec(m, v):
    return [sum(a * b for a, b in zip(row, v)) for row in m]


def design(rows, spec: ModelSpec):
    columns = ["(intercept)", spec.regressor]
    levels = {}
    for fe in spec.fixed_effects:
        values = sorted({str(r[fe]) for r in rows})
        levels[fe] = values[1:]
        columns += [f"C({fe})[{v}]" for v in values[1:]]
    X = []
    for r in rows:
        line = [1.0, float(r[spec.regressor])]
        for fe in spec.fixed_effects:
            line += [1.0 if str(r[fe]) == v else 0.0 for v in levels[fe]]
        X.append(line)
    y = [float(r[spec.outcome]) for r in rows]
    return X, y, columns


def _diagnostic(spec, reason, **extra):
    return {"status": "diagnostic", "reason": reason, "spec": spec.describe(), "estimand": ESTIMAND_NOTE, **extra}


def _covariance(X, residual_scores, bread, rows, spec, n, k, sigma2=None):
    if spec.cluster is None:
        if sigma2 is None:  # logit: inverse information
            return bread
        return [[v * sigma2 for v in row] for row in bread]
    groups = {}
    for row, score, r in zip(X, residual_scores, rows):
        g = groups.setdefault(str(r[spec.cluster]), [0.0] * k)
        for i in range(k):
            g[i] += row[i] * score
    meat = [[0.0] * k for _ in range(k)]
    for g in groups.values():
        for i in range(k):
            for j in range(k):
                meat[i][j] += g[i] * g[j]
    G = len(groups)
    correction = (G / (G - 1)) * ((n - 1) / (n - k)) if spec.model == "ols" else G / (G - 1)
    left = [[sum(bread[i][m] * meat[m][j] for m in range(k)) for j in range(k)] for i in range(k)]
    return [[correction * sum(left[i][m] * bread[m][j] for m in range(k)) for j in range(k)] for i in range(k)]


def fit(rows, spec: ModelSpec):
    rows = sorted(rows, key=lambda r: r["record_id"])  # input order never matters
    n = len(rows)
    if n == 0:
        return _diagnostic(spec, "empty_cohort")
    if n < spec.min_rows:
        return _diagnostic(spec, "too_few_rows", n=n)
    outcomes = {r[spec.outcome] for r in rows}
    if len(outcomes) < 2:
        return _diagnostic(spec, "no_outcome_variation", n=n)
    if len({r[spec.regressor] for r in rows}) < 2:
        return _diagnostic(spec, "no_regressor_variation", n=n)
    if spec.cluster is not None:
        clusters = len({str(r[spec.cluster]) for r in rows})
        if clusters < spec.min_clusters:
            return _diagnostic(spec, "too_few_clusters", n=n, clusters=clusters)
    X, y, columns = design(rows, spec)
    k = len(columns)
    if n <= k:
        return _diagnostic(spec, "more_parameters_than_rows", n=n, k=k)
    if spec.model == "ols":
        xtx = _xtwx(X, [1.0] * n)
        bread = _inverse(xtx)
        if bread is None:
            return _diagnostic(spec, "singular_design", n=n, k=k)
        xty = [sum(row[i] * yi for row, yi in zip(X, y)) for i in range(k)]
        beta = _matvec(bread, xty)
        residuals = [yi - sum(a * b for a, b in zip(row, beta)) for row, yi in zip(X, y)]
        sigma2 = sum(e * e for e in residuals) / (n - k)
        cov = _covariance(X, residuals, bread, rows, spec, n, k, sigma2)
        iterations = 1
    else:
        beta = [0.0] * k
        iterations = 0
        converged = False
        for iterations in range(1, 51):
            eta = [sum(a * b for a, b in zip(row, beta)) for row in X]
            p = [1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, e)))) for e in eta]
            w = [pi * (1.0 - pi) for pi in p]
            info = _xtwx(X, w)
            bread = _inverse(info)
            if bread is None:
                return _diagnostic(spec, "singular_or_separated_design", n=n, k=k)
            grad = [sum(row[i] * (yi - pi) for row, yi, pi in zip(X, y, p)) for i in range(k)]
            step = _matvec(bread, grad)
            beta = [b + s for b, s in zip(beta, step)]
            if max(abs(s) for s in step) < 1e-9:
                converged = True
                break
        if not converged or max(abs(b) for b in beta) > 15:
            return _diagnostic(spec, "separation_or_nonconvergence", n=n, k=k, iterations=iterations)
        eta = [sum(a * b for a, b in zip(row, beta)) for row in X]
        p = [1.0 / (1.0 + math.exp(-e)) for e in eta]
        if min(p) < 1e-8 or max(p) > 1 - 1e-8:
            return _diagnostic(spec, "quasi_separation", n=n, k=k)
        bread = _inverse(_xtwx(X, [pi * (1 - pi) for pi in p]))
        residuals = [yi - pi for yi, pi in zip(y, p)]
        cov = _covariance(X, residuals, bread, rows, spec, n, k)
    index = 1
    se = math.sqrt(max(cov[index][index], 0.0))
    if not math.isfinite(se) or se == 0.0:
        return _diagnostic(spec, "degenerate_standard_error", n=n, k=k)
    estimate = beta[index]
    result = {
        "status": "estimated", "spec": spec.describe(), "estimand": ESTIMAND_NOTE, "n": n, "k": k,
        "clusters": len({str(r[spec.cluster]) for r in rows}) if spec.cluster else None,
        "iterations": iterations, "term": spec.regressor,
        "estimate": round(estimate, 10), "std_error": round(se, 10),
        "ci95": [round(estimate - Z95 * se, 10), round(estimate + Z95 * se, 10)],
        "z": round(estimate / se, 10),
    }
    if spec.model == "logit":
        result["odds_ratio"] = round(math.exp(estimate), 10)
    return result


def exposure_normalized_rates(rows, outcome="wrongdoing_any"):
    """Per-term and pooled-by-party rates per completed term-year (equal-duration windows)."""
    from foia_bias.analysis.cohort import term_exposure_years
    years = term_exposure_years()
    per_term = {}
    for r in rows:
        bucket = per_term.setdefault(r["term_id"], {"documents": 0, "outcome": 0, "party": r["admin_party"]})
        bucket["documents"] += 1
        bucket["outcome"] += r[outcome]
    pooled = {}
    for term, bucket in sorted(per_term.items()):
        bucket["exposure_years"] = years[term]
        bucket["documents_per_year"] = round(bucket["documents"] / years[term], 10)
        bucket["outcome_per_year"] = round(bucket["outcome"] / years[term], 10)
        p = pooled.setdefault(bucket["party"], {"documents": 0, "outcome": 0, "exposure_years": 0, "terms": 0})
        p["documents"] += bucket["documents"]
        p["outcome"] += bucket["outcome"]
        p["exposure_years"] += years[term]
        p["terms"] += 1
    for p in pooled.values():
        p["documents_per_year"] = round(p["documents"] / p["exposure_years"], 10)
        p["outcome_per_year"] = round(p["outcome"] / p["exposure_years"], 10)
        p["outcome_share"] = round(p["outcome"] / p["documents"], 10) if p["documents"] else None
    return {"per_term": per_term, "pooled_by_incumbent_party": pooled,
            "note": "Rates use completed-term exposure years; raw totals across unequal exposure are not compared."}


def analysis_report(cohort, specs=None):
    specs = specs or [wrongdoing_spec(), favorability_spec()]
    results = [fit(cohort["rows"], spec) for spec in specs]
    body = {"cohort_version": cohort["cohort_version"], "cohort_config_sha256": cohort["config_sha256"],
            "row_count": cohort["row_count"], "exclusions": cohort["exclusions"], "models": results,
            "exposure": exposure_normalized_rates(cohort["rows"]) if cohort["rows"] else None,
            "estimand": ESTIMAND_NOTE, "research_eligible": False}
    return dict(body, report_sha256=hashlib.sha256(canonical(body).encode()).hexdigest())
