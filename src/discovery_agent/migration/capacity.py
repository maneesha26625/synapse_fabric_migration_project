"""A Fabric capacity assessment from what discovery found: a starting point with its evidence, never a verdict.

Fabric runs every workload of a workspace on one shared capacity, measured in capacity units (CU). The
assessment reads what the Synapse estate actually uses and turns each part into CU with a stated basis:

* **Spark**: the largest Spark pool at full scale, in vCores. Fabric's documented rate is
  1 CU = 2 Spark vCores (and Spark may burst to 3x that for a while).
* **SQL**: the dedicated pool's service level (DWU). Microsoft publishes no DWU-to-CU conversion, so
  ``CU_PER_DW100`` is an **assumption** the assessment states, to be replaced by what a pilot measures in
  the Fabric Capacity Metrics app.
* **Data volume, concurrency and schedules** are evidence that raises or lowers confidence: the peak
  number of SQL requests running at once (from the request history), the busiest hour of schedule
  triggers, and the data that the first load moves.

It gives two figures: each workload on its own (when they run at different times) and both at once.
The recommendation is the smallest F SKU that covers the larger of the two workloads, with the "both at
once" SKU shown beside it. When the target workspace's capacity is known, it says whether that fits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: The F SKUs and their capacity units.
SKUS = (("F2", 2), ("F4", 4), ("F8", 8), ("F16", 16), ("F32", 32), ("F64", 64), ("F128", 128), ("F256", 256),
        ("F512", 512), ("F1024", 1024), ("F2048", 2048))
#: Documented: one capacity unit runs two Spark vCores.
SPARK_VCORES_PER_CU = 2
#: An assumption, stated with every assessment: how many CU one DW100c of a dedicated pool needs. Calibrate with a pilot.
CU_PER_DW100 = 6
#: Synapse Spark node sizes in vCores.
NODE_VCORES = {"small": 4, "medium": 8, "large": 16, "xlarge": 32, "xxlarge": 64}
#: More SQL requests than this at once has queued on a mid-sized pool; it is a reason to size up, not a rule.
HIGH_CONCURRENCY = 32

_DWU = re.compile(r"DW(\d+)c?", re.IGNORECASE)


def sku_for(cu: float) -> Optional[str]:
    """The smallest F SKU with at least ``cu`` capacity units, or None beyond the largest."""
    for name, units in SKUS:
        if units >= cu:
            return name
    return None


def units_of(sku: Optional[str]) -> Optional[int]:
    if not sku:
        return None
    return dict(SKUS).get(str(sku).upper())


def dwu_of(sku: Optional[Mapping[str, Any]]) -> Optional[int]:
    """``{"name": "DW1000c"}`` -> 1000."""
    m = _DWU.search(str((sku or {}).get("name") or ""))
    return int(m.group(1)) if m else None


def spark_vcores(pool: Mapping[str, Any]) -> int:
    """A Spark pool's vCores at full scale (autoscale maximum, or its fixed node count)."""
    size = NODE_VCORES.get(str(pool.get("nodeSize") or "").lower(), 0)
    auto = pool.get("autoScale") or {}
    nodes = int((auto.get("maxNodeCount") if auto.get("enabled") else None) or pool.get("nodeCount") or 0)
    return size * nodes


def _busiest_hour(triggers: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """How many schedule triggers start in the same hour of the day, at most, from their recurrence."""
    by_hour: Dict[int, int] = {}
    counted = 0
    for t in triggers:
        rec = (((t.get("properties") or {}).get("typeProperties") or {}).get("recurrence") or {})
        if not rec:
            continue
        hours = ((rec.get("schedule") or {}).get("hours")) or []
        if rec.get("frequency") in ("Minute", "Hour"):
            hours = list(range(24))
        elif not hours:
            start = str(rec.get("startTime") or "")
            hours = [int(start[11:13])] if len(start) >= 13 and start[11:13].isdigit() else []
        counted += 1
        for h in set(int(x) for x in hours if str(x).isdigit() or isinstance(x, int)):
            by_hour[h] = by_hour.get(h, 0) + 1
    if not by_hour:
        return {"schedules": counted, "busiestHour": None, "atOnce": 0}
    hour, n = max(by_hour.items(), key=lambda kv: (kv[1], -kv[0]))
    return {"schedules": counted, "busiestHour": hour, "atOnce": n}


def assess(estate: Any = None, pool_sku: Optional[Mapping[str, Any]] = None, spark_pools: Sequence[Mapping[str, Any]] = (),
           triggers: Sequence[Mapping[str, Any]] = (), target_sku: Optional[str] = None) -> Dict[str, Any]:
    """The capacity assessment. Pure: everything it needs is passed in."""
    evidence: List[str] = []
    caveats: List[str] = [
        f"SQL is converted at an assumed {CU_PER_DW100} CU per DW100c: Microsoft publishes no conversion. Run a pilot and read the "
        "Fabric Capacity Metrics app before choosing the production SKU.",
        "Fabric smooths usage over time and lets work borrow capacity briefly, so short peaks fit a smaller SKU than their size suggests.",
    ]
    dwu = dwu_of(pool_sku)
    sql_cu = round(dwu / 100 * CU_PER_DW100) if dwu else None
    if dwu:
        evidence.append(f"The dedicated pool runs at DW{dwu}c: about {sql_cu} CU on the stated assumption.")
    vcores = max((spark_vcores(p) for p in spark_pools), default=0)
    spark_cu = (vcores + SPARK_VCORES_PER_CU - 1) // SPARK_VCORES_PER_CU if vcores else None
    if vcores:
        biggest = max(spark_pools, key=spark_vcores)
        evidence.append(f"The largest Spark pool ({biggest.get('name') or 'unnamed'}) reaches {vcores} vCores: {spark_cu} CU "
                        f"(1 CU = {SPARK_VCORES_PER_CU} Spark vCores).")
    data: Dict[str, Any] = {}
    workload: Dict[str, Any] = {}
    if estate is not None:
        if getattr(estate, "sizes_readable", False):
            largest = sorted(estate.sizes, key=lambda s: -s.reserved_kb)[:5]
            data = {"gb": round(estate.total_kb / 1024 / 1024, 2), "rows": estate.total_rows,
                    "largest": [{"table": f"{s.schema}.{s.table}", "gb": round(s.reserved_kb / 1024 / 1024, 2), "rows": s.rows} for s in largest]}
            evidence.append(f"{data['gb']:,} GB in {len(estate.sizes):,} tables ({estate.total_rows:,} rows) move in the first load.")
        else:
            caveats.append("Table sizes could not be read (VIEW DATABASE STATE): the first load's size is unknown.")
        w = getattr(estate, "workload", None)
        if w is not None:
            workload = {"peakConcurrency": w.peak_concurrency, "requests": w.requests, "windowStart": w.window_start, "windowEnd": w.window_end}
            if w.requests:
                evidence.append(f"At most {w.peak_concurrency} SQL requests ran at once, in {w.requests:,} recent requests "
                                f"({(w.window_start or '')[:16]} to {(w.window_end or '')[:16]}).")
            if w.peak_concurrency > HIGH_CONCURRENCY and sql_cu:
                sql_cu = round(sql_cu * 1.5)
                evidence.append(f"Peak concurrency above {HIGH_CONCURRENCY}: SQL sized up by half, so queries do not queue.")
        else:
            caveats.append("The request history could not be read (VIEW DATABASE STATE): concurrency is unknown.")
    schedules = _busiest_hour(triggers)
    if schedules["atOnce"]:
        evidence.append(f"{schedules['atOnce']} schedule trigger(s) start in the busiest hour ({schedules['busiestHour']:02d}:00 UTC).")
    alone = max(sql_cu or 0, spark_cu or 0)
    together = (sql_cu or 0) + (spark_cu or 0)
    recommended = sku_for(alone) if alone else None
    out: Dict[str, Any] = {
        "sql": {"dwu": dwu, "cu": sql_cu, "basis": f"assumed {CU_PER_DW100} CU per DW100c" if dwu else None},
        "spark": {"vcores": vcores or None, "cu": spark_cu, "basis": f"1 CU = {SPARK_VCORES_PER_CU} Spark vCores" if vcores else None},
        "data": data or None, "workload": workload or None, "schedules": schedules,
        "recommendation": {
            "sku": recommended, "cu": units_of(recommended),
            "togetherSku": sku_for(together) if together else None,
            "why": ("The larger of the SQL and Spark workloads; if they run at the same time, "
                    f"{sku_for(together) or 'more than F2048'} covers both.") if recommended else
                   "Nothing measurable was discovered (no SQL pool size and no Spark pool): start small and measure.",
        },
        "evidence": evidence, "caveats": caveats,
    }
    target_cu = units_of(target_sku)
    if target_sku:
        fits = None if target_cu is None or not alone else target_cu >= alone
        out["target"] = {"sku": target_sku, "cu": target_cu, "fits": fits,
                         "detail": ("Not an F SKU (a trial or P SKU): compare it in the Capacity Metrics app." if target_cu is None
                                    else f"{target_sku} covers the larger workload." if fits
                                    else f"{target_sku} is smaller than the assessment's {recommended}: expect throttling at peak, "
                                         "or schedule SQL and Spark apart." if fits is False else "Nothing to compare against.")}
    return out
