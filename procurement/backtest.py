"""Бэктест: насколько точно сервис предсказывает спрос следующего месяца по сравнению с тем,
как это делается в Excel (среднее продаж за 12 месяцев).

Для каждой точки отсечения (последние N полных месяцев) данные обрезаются так, будто сегодня —
1-е число месяца M: продажи, накладные и сезонность известны только до M-1. Затем:
  - сервис: прогноз engine.calculate на весь месяц M (горизонт = длина месяца);
  - Excel:  среднее «сырых» месячных продаж за 12 месяцев до M.
Сравнение с фактом месяца M по всем артикулам с продажами:
  WAPE = sum|прогноз - факт| / sum(факт), смещение = sum(прогноз - факт) / sum(факт).
Отдельно считается «перезаказ» — сумма превышения прогноза над фактом: это запас,
который лёг бы на склад лишним.
"""
from __future__ import annotations

import calendar
import pickle
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd

import constants

from . import engine, loaders

CACHE = constants.BASE_DIR / "data" / "cache" / "backtest.pkl"


def _truncate(data: loaders.SupplierData, month: pd.Period) -> loaders.SupplierData:
    """Данные «на 1-е число месяца month»."""
    cut = month.start_time
    season = data.seasonality.copy()
    for y in season.index:
        for m in season.columns:
            if pd.Period(year=int(y), month=int(m), freq="M") >= month:
                season.loc[y, m] = np.nan
    monthly = data.monthly[data.monthly["month"] <= month].copy()
    monthly.loc[monthly["month"] == month, "sales_1c"] = 0.0  # продажи текущего месяца неизвестны
    return replace(data, transactions=data.transactions[data.transactions["date"] < cut],
                   monthly=monthly, seasonality=season, data_date=cut.date())


def run(n_months: int = 6) -> dict:
    result = {"created": str(date.today()), "suppliers": {}}
    for key in constants.SUPPLIERS:
        data = loaders.get_supplier(key)
        last_full = pd.Period(data.data_date, freq="M") - 1
        actual = data.monthly.pivot_table(index="code", columns="month", values="sales_1c", aggfunc="sum").fillna(0)
        rows = []
        for m in pd.period_range(last_full - (n_months - 1), last_full, freq="M"):
            days = calendar.monthrange(m.year, m.month)[1]
            fc = engine.calculate(_truncate(data, m), engine.Params(lead_time_days=1, review_days=days - 1))
            if fc.empty:
                continue
            fc = fc.set_index("code_1c")
            hist = [p for p in actual.columns if m - 12 <= p < m]
            naive = actual[hist].mean(axis=1) * days / 30.0
            codes = fc.index.intersection(actual.index)
            fact = actual.loc[codes, m]
            for label, pred in (("service", fc.loc[codes, "forecast"]), ("excel", naive.reindex(codes).fillna(0))):
                err = pred - fact
                rows.append({"month": str(m), "method": label, "fact": float(fact.sum()),
                             "abs_err": float(err.abs().sum()), "bias": float(err.sum()),
                             "over": float(err.clip(lower=0).sum()), "under": float((-err).clip(lower=0).sum()),
                             "sku": int(len(codes))})
        df = pd.DataFrame(rows)
        if df.empty:
            continue
        by = df.groupby("method")[["fact", "abs_err", "bias", "over", "under"]].sum()
        summary = {
            meth: {"wape": r.abs_err / r.fact if r.fact else None, "bias": r.bias / r.fact if r.fact else None,
                   "over": r.over, "under": r.under}
            for meth, r in by.iterrows()
        }
        per_month = []
        for mo, g in df.groupby("month"):
            g = g.set_index("method")
            per_month.append({"month": mo, "sku": int(g["sku"].iloc[0]), "fact": float(g["fact"].iloc[0]),
                              "service": float(g.loc["service", "abs_err"] / g.loc["service", "fact"]),
                              "excel": float(g.loc["excel", "abs_err"] / g.loc["excel", "fact"])})
        result["suppliers"][key] = {"name": data.name, "summary": summary, "per_month": per_month}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as fh:
        pickle.dump(result, fh)
    return result


def load() -> dict | None:
    if CACHE.exists():
        try:
            with open(CACHE, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            return None
    return None
