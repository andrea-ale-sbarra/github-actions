"""
Is a build fit to be analyzed, or did the CI break it?

On 2026-11-03 (run 9957) the CI broke: Commerce build 560288074 had 481
results not passed against a usual ~45, UM build 560288420 had 148 against
~15, and the Acceptance build of the same day 3255 failed against ~700.
Analyzing those builds would have opened a ticket for every broken shard,
and the closure pass would have closed as "not reproducible" every ticket
whose test simply did not run. This module tells such a build apart, so the
analyzer can stop before writing anything (see `analyze_testflow`).

The measure is the number of results NOT passed (failed + blocked + untested
+ test fix), not the failed count alone: after the analysis the script moves
the failed results it links to a ticket to BLOCKED, so on an analyzed build
"failed" only keeps what was left unlinked (build 556026987: 12 failed, 124
blocked). "Not passed" is the same before and after the analysis, so a fresh
build can be compared with analyzed ones.

A build is out of scale when its not-passed count is at least
OUT_OF_SCALE_RATIO times the median of the previous BASELINE_BUILDS builds
of the same routine AND at least OUT_OF_SCALE_MIN_EXCESS above it. Checked
on the last 30 builds of each routine (2026-09-24 .. 2026-11-05):
  - stops 9957 on both routines (x10.7 Commerce, x10.2 UM);
  - passes every other Commerce build (max x3.0);
  - passes every other UM build but two: 2026-10-07 and 2026-10-08
    (147 and 148, x12-14). Two runs in a row with the same count look like
    a real regression rather than the CI: the check stops them anyway, and
    deciding is left to a human (or the routine-triage skill), who looks at
    HOW the build fails. The check only says "this is not a normal build".
The absolute minimum keeps small routines quiet: UM went from 8 to 34 on
2026-10-27 (x4.2), which is not worth stopping a run for.

Acceptance has its own ratio: it runs ~19000 tests with ~950 not passed on
a normal day, so a broken CI moves it less in proportion. On its last 40
builds (2026-09-30 .. 2026-11-05) the normal ones stay within x0.8-x1.2;
2026-11-03 (run 31, same CI breakage as 9957) is x3.75, 2026-10-15 (11134
untested) x11.8. 2026-11-01 is x2.0 (1190 untested) and is not stopped:
borderline, left below the threshold until a case says otherwise. The
analyzer never analyzes Acceptance; the PR check reads its builds and uses
this measure to warn that a failure may come from the CI (pr_check.py).

Known limits:
  - a CI failure that drops whole shards from the build (fewer results,
    not more failures) is not detected: not seen in the 30 builds checked,
    so no rule was written for it.
  - the median comes from the Testray metrics endpoint, which lists the
    routine's builds mixed with Acceptance builds; BASELINE_FETCH_SIZE is
    sized so ~100 items still hold at least BASELINE_BUILDS of the routine.
"""

import statistics

from functools import lru_cache

from testray_api import ACCEPTANCE_ROUTINE_ID, TESTRAY_REST_URL, _get_json

BASELINE_BUILDS = 10
BASELINE_FETCH_SIZE = 100
OUT_OF_SCALE_RATIO = 5
OUT_OF_SCALE_RATIO_BY_ROUTINE = {ACCEPTANCE_ROUTINE_ID: 2.5}
OUT_OF_SCALE_MIN_EXCESS = 50


def _not_passed(metric):
    return (metric.get("total") or 0) - (metric.get("passed") or 0)


@lru_cache(maxsize=None)
def _routine_build_metrics(routine_id):
    """Metrics of the routine's builds, newest first, Acceptance filtered out."""
    url = (
        f"{TESTRAY_REST_URL}/testray-status-metrics/by-testray-routineId/"
        f"{routine_id}/testray-builds-metrics?pageSize={BASELINE_FETCH_SIZE}"
    )
    items = _get_json(url).get("items", []) or []
    return tuple(i for i in items if i.get("testrayRoutineId") == routine_id)


def assess_build(routine_id, build_id):
    """
    Compare `build_id` with the previous builds of `routine_id`.

    Returns a dict:
        build_id, build_name, total, not_passed, failed, blocked, untested,
        baseline_median, baseline_size, ratio, out_of_scale (bool)
    or None when the build is not in the metrics list (e.g. still importing)
    or there are fewer than 3 previous builds to compare with: in both cases
    the caller must not block the run on a missing measure.
    """
    metrics = _routine_build_metrics(routine_id)
    index = next(
        (i for i, m in enumerate(metrics) if m.get("testrayBuildId") == build_id),
        None,
    )
    if index is None:
        return None

    current = metrics[index]
    previous = metrics[index + 1:index + 1 + BASELINE_BUILDS]
    if len(previous) < 3:
        return None

    metric = current.get("testrayStatusMetric") or {}
    not_passed = _not_passed(metric)
    median = statistics.median(
        _not_passed(m.get("testrayStatusMetric") or {}) for m in previous
    )
    ratio = not_passed / max(median, 1)

    return {
        "build_id": build_id,
        "build_name": current.get("testrayBuildName"),
        "total": metric.get("total") or 0,
        "not_passed": not_passed,
        "failed": metric.get("failed") or 0,
        "blocked": metric.get("blocked") or 0,
        "untested": metric.get("untested") or 0,
        "baseline_median": median,
        "baseline_size": len(previous),
        "ratio": ratio,
        "out_of_scale": (
            ratio >= OUT_OF_SCALE_RATIO_BY_ROUTINE.get(routine_id, OUT_OF_SCALE_RATIO)
            and not_passed - median >= OUT_OF_SCALE_MIN_EXCESS
        ),
    }


def format_health(health):
    """One line: '481 of 1886 results not passed, usual 45 (x10.7)'."""
    return (
        f"{health['not_passed']} of {health['total']} results not passed, "
        f"usual {health['baseline_median']:g} (x{health['ratio']:.1f}, "
        f"median of the previous {health['baseline_size']} builds)"
    )
