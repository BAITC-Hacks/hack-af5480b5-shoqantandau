"""Запуск расчёта и сохранение результата в БД."""
from __future__ import annotations

import math

import constants

from . import engine, loaders
from .models import CalculationRun, OrderLine


def _clean(v):
    """JSON-совместимые значения (без NaN/inf/numpy-типов)."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def run_calculation(params: engine.Params, suppliers: list[str] | None = None) -> CalculationRun:
    keys = suppliers or list(constants.SUPPLIERS)
    run = CalculationRun.objects.create(supplier=",".join(keys), category=params.category,
                                        params=_clean(params.to_dict()))
    stats, lines = {}, []
    for key in keys:
        data = loaders.get_supplier(key)
        df = engine.calculate(data, params)
        to_order = df[df["qty_recommended"] > 0] if not df.empty else df
        stats[key] = {
            "name": data.name, "sku_calculated": len(df), "sku_to_order": len(to_order),
            "qty_total": int(to_order["qty_recommended"].sum()) if len(to_order) else 0,
            "high": int((to_order["urgency"] == "high").sum()) if len(to_order) else 0,
            "one_offs": int((df["one_off_removed"] > 0).sum()) if len(df) else 0,
            "stockouts": int((df["stockout_added"] > 0).sum()) if len(df) else 0,
            "lead_time_days": int(params.lead_time_days or data.lead_time_days),
            "warnings": data.warnings,
        }
        for r in df.itertuples(index=False):
            lines.append(OrderLine(
                run=run, supplier=key, code_1c=r.code_1c, article=str(r.article or ""), name=r.name,
                category=str(r.category), stock=r.stock, in_transit=r.in_transit,
                regular_demand=r.regular_demand, forecast=r.forecast, safety_stock=r.safety_stock,
                moq=r.moq, qty_recommended=int(r.qty_recommended), urgency=r.urgency,
                days_of_cover=float(r.days_of_cover), reason=r.reason,
                details=_clean({**r.details, "qty_raw": int(r.qty_raw), "unit": r.unit,
                                "one_off_removed": r.one_off_removed, "stockout_added": r.stockout_added}),
            ))
    OrderLine.objects.bulk_create(lines, batch_size=1000)
    run.stats = _clean(stats)
    run.save(update_fields=["stats"])
    return run
