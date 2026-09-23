"""Read the six delivered workbooks through the production loader and engine."""
from django.test import SimpleTestCase
import numpy as np

from . import engine, loaders


class SyntheticCompany(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data = loaders.load_supplier('techno')
        cls.frame = engine.calculate(cls.data).set_index('code_1c')

    def test_transactions_reconcile_every_sku_month(self):
        tx = self.data.transactions
        totals = tx.groupby([tx.code, tx.date.dt.to_period('M')]).qty.sum()
        monthly = self.data.monthly.set_index(['code', 'month']).sales_1c
        self.assertTrue(np.allclose(totals.reindex(monthly.index).fillna(0), monthly))
        self.assertEqual(len(self.data.items), 13)
        self.assertEqual(str(self.data.data_date), '2026-09-22')

    def test_current_stock_and_dated_shipments_are_both_loaded(self):
        self.assertFalse(self.data.items.stock_estimated.any())
        self.assertEqual(self.data.items.loc['3301001_', 'stock_now'], 5000)
        self.assertEqual(self.frame.loc['3301007_', 'details']['late_transit'], 300)

    def test_summer_and_winter_have_opposite_seasons(self):
        summer = engine.seasonal_comparison(self.frame.loc['1101001_'], self.data.data_date)
        winter = engine.seasonal_comparison(self.frame.loc['2201001_'], self.data.data_date)
        self.assertGreater(summer[6]['demand'], summer[0]['demand'] * 2)
        self.assertGreater(summer[6]['qty'], summer[0]['qty'])
        self.assertGreater(winter[0]['demand'], winter[6]['demand'] * 2)
        self.assertGreater(winter[0]['qty'], winter[6]['qty'])

    def test_stock_and_arrival_timing_change_actions(self):
        self.assertEqual(self.frame.loc['3301001_', 'qty_recommended'], 0)
        self.assertGreater(self.frame.loc['3301002_', 'qty_recommended'], 0)
        self.assertEqual(self.frame.loc['3301003_', 'qty_recommended'], 0)
        self.assertEqual(self.frame.loc['3301003_', 'urgency'], 'low')
        self.assertEqual(self.frame.loc['3301004_', 'qty_recommended'], 0)
        self.assertEqual(self.frame.loc['3301004_', 'urgency'], 'high')
        self.assertGreater(self.frame.loc['3301007_', 'qty_recommended'], 0)

    def test_known_bulk_client_purchase_is_removed(self):
        clean = self.frame.loc['3301006_']
        raw = engine.calculate(self.data, engine.Params(codes=['3301006_'], use_outliers=False)).iloc[0]
        self.assertGreater(clean.one_off_removed, 1000)
        self.assertLess(clean.qty_recommended, raw.qty_recommended)
        events = clean['details']['one_offs']
        self.assertTrue(any(e['qty'] == 1200 for e in events))

    def test_stockout_compensation_and_growth(self):
        raw = engine.calculate(self.data, engine.Params(codes=['3301005_'], use_stockout=False)).iloc[0]
        self.assertGreater(self.frame.loc['3301005_', 'forecast'], raw.forecast)
        self.assertGreater(self.frame.loc['4401001_', 'details']['growth_year'], 1)
        self.assertLess(self.frame.loc['5501001_', 'details']['growth_year'], 1)

    def test_seasonal_scenarios_reconcile_to_formula(self):
        row = self.frame.loc['1101001_']
        comparison = engine.seasonal_comparison(row, self.data.data_date)
        safety = engine.Params().service_z * row['details']['sigma']
        for scenario in comparison:
            expected = max(0, np.ceil((scenario['demand'] + safety - row.stock) / row.moq) * row.moq)
            self.assertEqual(scenario['qty'], expected)
