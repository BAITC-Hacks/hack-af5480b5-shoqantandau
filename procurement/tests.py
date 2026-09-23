"""Проверки обязательных требований кейса (must have 1–5) на синтетических данных.

Запуск: python manage.py test procurement
"""
from datetime import date

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from .engine import Params, calculate
from .loaders import SupplierData

TODAY = date(2026, 9, 22)
MONTHS = list(pd.period_range("2024-01", "2026-09", freq="M"))
FLAT_SEASON = pd.DataFrame({m: [100.0, 100.0, 100.0] for m in range(1, 13)}, index=[2024, 2025, 2026])


def make_data(skus: dict, season: pd.DataFrame = FLAT_SEASON) -> SupplierData:
    """skus: code -> {monthly: [33 числа продаж], stock: [33 остатка на начало], stock_now, in_transit, moq,
    lines: доп. строки накладных [(date, doc, qty)]}. Продажи раскладываются по 4 накладным в месяц."""
    mrows, trows, irows = [], [], []
    for code, s in skus.items():
        for i, per in enumerate(MONTHS):
            q = s["monthly"][i]
            st = s.get("stock", [100.0] * len(MONTHS))[i]
            mrows.append({"code": code, "month": per, "sales_1c": q, "stock_open": st})
            if q > 0 and per < pd.Period(TODAY, freq="M"):
                for j in range(4):
                    trows.append({"date": per.start_time + pd.Timedelta(days=3 + 7 * j), "doc": f"{code}-{per}-{j}",
                                  "code": code, "name": code, "warehouse": "Алматы", "qty": q / 4})
        for d, doc, q in s.get("lines", []):
            trows.append({"date": pd.Timestamp(d), "doc": doc, "code": code, "name": code, "warehouse": "Алматы", "qty": q})
        name = s.get("name", f"Товар {code}")
        irows.append({"code": code, "name": name, "article": code, "unit": "шт", "category": s.get("category", "0302"),
                      "marked": "!!!" in name,
                      "abc_class": "", "moq": s.get("moq", 1), "in_transit": s.get("in_transit", 0.0),
                      "next_arrival": pd.Timestamp("2026-10-01") if s.get("in_transit") else pd.NaT,
                      "stock_now": s.get("stock_now", 10.0)})
    items = pd.DataFrame(irows).set_index("code")
    return SupplierData(key="test", name="Тест", transactions=pd.DataFrame(trows), monthly=pd.DataFrame(mrows),
                        items=items, seasonality=season, orders=pd.DataFrame(), data_date=TODAY)


def qty(df, code):
    return int(df.set_index("code_1c").loc[code, "qty_recommended"])


def row(df, code):
    return df.set_index("code_1c").loc[code]


class MustHave1AllSourcesUsed(SimpleTestCase):
    """Изменение любого источника данных отражается на результате."""

    def setUp(self):
        self.data = make_data({"A": {"monthly": [40.0] * 33, "stock_now": 10}})
        self.base = qty(calculate(self.data, Params(lead_time_days=30)), "A")

    def test_base_positive(self):
        self.assertGreater(self.base, 0)

    def test_in_transit_reduces_order(self):
        r = calculate(self.data, Params(lead_time_days=30, overrides={"A": {"in_transit": 30}}))
        self.assertEqual(qty(r, "A"), self.base - 30)

    def test_stock_reduces_order(self):
        r = calculate(self.data, Params(lead_time_days=30, overrides={"A": {"stock_now": 25}}))
        self.assertEqual(qty(r, "A"), self.base - 15)

    def test_growth_plan_increases_order(self):
        r = calculate(self.data, Params(lead_time_days=30, growth_plan_pct=30))
        self.assertGreater(qty(r, "A"), self.base)

    def test_lead_time_increases_order(self):
        r = calculate(self.data, Params(lead_time_days=60))
        self.assertGreater(qty(r, "A"), self.base)

    def test_moq_rounding(self):
        r = calculate(make_data({"A": {"monthly": [40.0] * 33, "stock_now": 10, "moq": 50}}), Params(lead_time_days=30))
        self.assertEqual(qty(r, "A") % 50, 0)
        self.assertGreaterEqual(qty(r, "A"), self.base)

    def test_category_filter(self):
        self.assertTrue(calculate(self.data, Params(category="9999")).empty)
        self.assertFalse(calculate(self.data, Params(category="0302")).empty)


class MustHave1CategoryMatters(SimpleTestCase):
    """Группа товаров участвует в расчёте: смена группы меняет сезонность и количество."""

    def test_category_changes_result(self):
        pattern = [0.5, 0.5, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 1.8, 0.5, 0.3]  # пик осенью
        skus = {f"F{i}": {"monthly": [30.0] * 33, "category": "1111"} for i in range(6)}
        skus.update({f"S{i}": {"monthly": [30.0 * pattern[p.month - 1] for p in MONTHS], "category": "2222"} for i in range(6)})
        skus["A"] = {"monthly": [8.0] * 33, "category": "1111", "stock_now": 0}  # мало истории для своей сезонности
        data = make_data(skus)
        p = dict(lead_time_days=20, review_days=20, use_growth=False, codes=["A"])
        flat = qty(calculate(data, Params(**p)), "A")
        moved = qty(calculate(data, Params(**p, overrides={"A": {"category": "2222"}})), "A")
        self.assertGreater(moved, flat)


class MustHave2Seasonality(SimpleTestCase):
    """Для сезонного товара прогноз отражает сезон, а не среднее по истории."""

    def test_seasonal_peak(self):
        pattern = [0.5, 0.5, 0.7, 1.0, 1.2, 1.4, 1.5, 1.5, 1.4, 1.3, 0.6, 0.4]  # пик летом-осенью
        sales = [100 * pattern[p.month - 1] for p in MONTHS]
        season = pd.DataFrame({m: [1000 * pattern[m - 1]] * 3 for m in range(1, 13)}, index=[2024, 2025, 2026])
        data = make_data({"S": {"monthly": sales, "stock_now": 0}}, season)
        flat_avg = np.mean(sales[-13:-1])  # среднее за 12 полных месяцев
        p = dict(lead_time_days=20, review_days=20, use_growth=False)
        on = row(calculate(data, Params(**p)), "S")
        off = row(calculate(data, Params(**p, use_seasonality=False)), "S")
        # горизонт 40 дн. — конец сентября и октябрь, сезонный коэффициент ~1.3: прогноз выше среднего
        self.assertGreater(on["forecast"], off["forecast"] * 1.2)
        self.assertGreater(on["forecast"], flat_avg / 30 * 40 * 1.2)

    def test_low_season_below_average(self):
        pattern = [0.5, 0.5, 0.7, 1.0, 1.2, 1.4, 1.5, 1.5, 1.4, 1.3, 0.6, 0.4]
        season = pd.DataFrame({m: [1000 * pattern[m - 1]] * 3 for m in range(1, 13)}, index=[2024, 2025, 2026])
        data = make_data({"S": {"monthly": [100 * pattern[p.month - 1] for p in MONTHS]}}, season)
        # горизонт 90 дн. захватывает ноябрь–декабрь: сезонность тянет прогноз вниз относительно пика
        on = row(calculate(data, Params(lead_time_days=60, review_days=30, use_growth=False)), "S")
        off = row(calculate(data, Params(lead_time_days=60, review_days=30, use_growth=False, use_seasonality=False)), "S")
        self.assertLess(on["forecast"], off["forecast"])


class MustHave3Stockout(SimpleTestCase):
    """При зафиксированном дефиците потребность выше, чем по «сырым» продажам."""

    def test_stockout_increases_need(self):
        sales = [30.0] * 33
        stock = [100.0] * 33
        for i in (-4, -3):          # июнь и июль 2026 — товара нет, продаж почти нет
            sales[i] = 2.0
            stock[i] = 0.0
        data = make_data({"B": {"monthly": sales, "stock": stock, "stock_now": 5}})
        with_so = row(calculate(data, Params(lead_time_days=30)), "B")
        without = row(calculate(data, Params(lead_time_days=30, use_stockout=False)), "B")
        self.assertGreater(with_so["qty_recommended"], without["qty_recommended"])
        self.assertIn("Товара не было", with_so["reason"])


class MustHave4OneOffOrders(SimpleTestCase):
    """Искусственный разовый крупный заказ не раздувает регулярную потребность."""

    def test_injected_big_order_ignored(self):
        data = make_data({"C": {"monthly": [20.0] * 33, "stock_now": 5}})
        base = qty(calculate(data, Params(lead_time_days=30)), "C")
        big = [{"code": "C", "qty": 2000, "date": "2026-06-15", "doc": "ТЕСТ-1"}]
        clean = row(calculate(data, Params(lead_time_days=30, test_orders=big)), "C")
        raw = row(calculate(data, Params(lead_time_days=30, test_orders=big, use_outliers=False)), "C")
        self.assertLessEqual(abs(clean["qty_recommended"] - base), max(2, 0.1 * base))
        self.assertGreater(raw["qty_recommended"], base * 2)
        self.assertIn("ТЕСТ-1", clean["reason"])


    def test_spike_only_in_monthly_report_capped(self):
        """Эксперт может поднять продажи месяца в отчёте 1С, не добавляя накладную."""
        data = make_data({"C": {"monthly": [20.0] * 33, "stock_now": 5}})
        base = qty(calculate(data, Params(lead_time_days=30)), "C")
        spike = [{"code": "C", "qty": 2000, "date": "2026-06-15", "doc": "ТЕСТ-2", "monthly_only": True}]
        r = row(calculate(data, Params(lead_time_days=30, test_orders=spike)), "C")
        self.assertLessEqual(abs(r["qty_recommended"] - base), max(3, 0.15 * base))
        self.assertIn("Сглажены всплески", r["reason"])


class StockoutEdgeCases(SimpleTestCase):
    """Долгое отсутствие товара и пометка «!!!» — спрос не выдумываем, позицию помечаем."""

    def test_long_stockout_not_restored(self):
        sales = [30.0] * 26 + [0.0] * 7
        stock = [100.0] * 26 + [0.0] * 7
        data = make_data({"D": {"monthly": sales, "stock": stock, "stock_now": 0}})
        r = row(calculate(data, Params(lead_time_days=30)), "D")
        self.assertTrue(r["needs_review"])
        self.assertEqual(r["stockout_added"], 0)
        self.assertIn("подряд", r["reason"])

    def test_marked_item_not_proposed(self):
        data = make_data({"E": {"monthly": [30.0] * 33, "stock_now": 0, "name": "Розетка Прима (96) !!!"}})
        r = row(calculate(data, Params(lead_time_days=30)), "E")
        self.assertEqual(r["qty_recommended"], 0)
        self.assertTrue(r["needs_review"])
        self.assertIn("Расчётно нужно", r["reason"])


class MustHave5GroupedWithReasons(SimpleTestCase):
    """Каждая строка с обоснованием, список делится по поставщикам."""

    def test_reasons_and_supplier(self):
        data = make_data({"A": {"monthly": [40.0] * 33}, "B": {"monthly": [5.0] * 33}})
        r = calculate(data)
        self.assertTrue((r["reason"].str.len() > 20).all())
        self.assertTrue(r["reason"].str.contains("Прогноз на").all())
        self.assertEqual(set(r["supplier"]), {"test"})
