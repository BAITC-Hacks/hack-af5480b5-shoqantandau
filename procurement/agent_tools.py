"""Read-only agent tools. All quantities come from the procurement engine."""
import math
import pandas as pd
from django.urls import reverse
from . import engine, loaders
from .services import _clean


def tool(name, description, properties):
    return {'type': 'function', 'name': name, 'description': description, 'strict': True,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(properties), 'additionalProperties': False}}


TOOLS = [
    tool('find_products', 'Найти товар выбранного поставщика по коду или части названия.',
         {'query': {'type': 'string', 'description': 'Код 1С или часть названия'}}),
    tool('list_risks', 'Обзор расчёта и первые 10 товаров: urgent — риск дефицита; surplus — запас более 180 дней; all — рекомендации.',
         {'kind': {'type': 'string', 'enum': ['urgent', 'surplus', 'all']}}),
    tool('get_product', 'Получить текущий расчёт одного товара, остатки, даты поступлений и причины.',
         {'code': {'type': 'string'}}),
    tool('simulate_product', 'Пересчитать товар при новых условиях и сравнить с базой. Не сохраняет и не утверждает заказ. null оставляет базовый параметр.',
         {'code': {'type': 'string'}, 'lead_days': {'type': ['integer', 'null']},
          'review_days': {'type': ['integer', 'null']}, 'growth_pct': {'type': ['number', 'null']},
          'stock': {'type': ['number', 'null']}, 'in_transit': {'type': ['number', 'null']},
          'seasonality': {'type': ['boolean', 'null']}}),
    tool('compare_seasons', 'Сравнить 12 сезонных сценариев по 30 дней при одном текущем остатке, без роста и поступлений. Это не календарный план закупок.',
         {'code': {'type': 'string'}}),
]
SCHEMAS = {t['name']: t['parameters'] for t in TOOLS}
LABELS = {'find_products': 'Поиск товаров', 'list_risks': 'Анализ рисков',
          'get_product': 'Расчёт товара', 'simulate_product': 'Сценарный пересчёт',
          'compare_seasons': 'Сравнение зимы и лета'}


def validate(name, args):
    if name not in SCHEMAS or not isinstance(args, dict):
        raise ValueError('Неизвестный инструмент или неверные аргументы.')
    schema = SCHEMAS[name]
    if set(args) != set(schema['properties']):
        raise ValueError('Неполный или лишний набор аргументов.')
    for key, value in args.items():
        spec = schema['properties'][key]
        types = spec['type'] if isinstance(spec['type'], list) else [spec['type']]
        actual = 'null' if value is None else 'boolean' if isinstance(value, bool) else 'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else 'string' if isinstance(value, str) else 'other'
        if actual not in types and not (actual == 'integer' and 'number' in types):
            raise ValueError(f'Неверный тип поля {key}.')
        if isinstance(value, str) and len(value) > 160:
            raise ValueError('Слишком длинный запрос инструмента.')
        if isinstance(value, (int, float)) and (abs(value) > 1e9 or not math.isfinite(value)):
            raise ValueError('Число должно быть конечным.')
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError('Недопустимое значение.')


class Context:
    def __init__(self, supplier, previous_scenarios=None):
        self.data = loaders.get_supplier(supplier)
        self._frame = None
        self.sources = {}
        self.scenario_state = dict(previous_scenarios or {})

    @property
    def frame(self):
        if self._frame is None:
            self._frame = engine.calculate(self.data).set_index('code_1c', drop=False)
        return self._frame

    def row(self, code):
        if code not in self.frame.index:
            raise ValueError('Товар не найден либо недостаточно продаж для расчёта. Используйте поиск.')
        return self.frame.loc[code]

    def source(self, code):
        item = self.data.items.loc[code]
        value = {'code': code, 'name': str(item['name']),
                 'url': reverse('procurement:sku_detail', args=[self.data.key, code])}
        self.sources[code] = value
        return value

    def summary(self, row):
        d = row['details']
        result = {**self.source(str(row.code_1c)),
                  'recommended_quantity': int(row.qty_recommended), 'unit': row.unit,
                  'stock': float(row.stock), 'stock_estimated': bool(d['stock_estimated']),
                  'in_transit': float(row.in_transit), 'eligible_transit': d['eligible_transit'],
                  'late_transit': d['late_transit'], 'forecast': round(float(row.forecast), 2),
                  'safety_stock': round(float(row.safety_stock), 2), 'order_multiple': float(row.moq),
                  'lead_days': d['lead'], 'horizon_days': d['horizon'], 'urgency': row.urgency,
                  'first_shortage_day': d['first_shortage_day'],
                  'days_of_cover': None if row.days_of_cover >= 9999 else round(float(row.days_of_cover), 1),
                  'one_off_removed': round(float(row.one_off_removed), 2),
                  'stockout_added': round(float(row.stockout_added), 2),
                  'annual_growth_factor': d['growth_year'], 'shipments': d['shipments'], 'warnings': d['flags']}
        return _clean(result)

    def call(self, name, args):
        validate(name, args)
        if name == 'find_products':
            query = args['query'].strip().casefold()
            if not query:
                raise ValueError('Введите код или часть названия.')
            items = self.data.items
            matches = [str(code) for code, item in items.iterrows()
                       if query in str(code).casefold() or query in str(item['name']).casefold()]
            return {'total_matches': len(matches), 'products': [self.source(c) for c in matches[:10]]}
        if name == 'list_risks':
            frame = self.frame
            kind = args['kind']
            selected = frame[frame.urgency == 'high'] if kind == 'urgent' else frame[
                (frame.days_of_cover > 180) & (frame.stock > 0)] if kind == 'surplus' else frame
            return {'data_date': str(self.data.data_date), 'supplier': self.data.name,
                    'calculated_skus': len(frame), 'matching_skus': len(selected),
                    'warnings': self.data.warnings, 'shown': min(10, len(selected)),
                    'products': [self.summary(row) for _, row in selected.head(10).iterrows()]}
        code = args['code']
        row = self.row(code)
        if name == 'get_product':
            return {'data_date': str(self.data.data_date), 'product': self.summary(row),
                    'data_warnings': self.data.warnings,
                    'basis': 'Новый расчёт по текущим файлам и стандартным настройкам. Не учитывает ручные правки сохранённых заказов.'}
        if name == 'compare_seasons':
            return {'product': self.source(code), 'stock': float(row.stock),
                    'data_date': str(self.data.data_date), 'unit': str(row.unit),
                    'conditions': 'Одинаковые 30 дней, текущий остаток, кратность и страховой запас; рост и поступления отключены. Не календарный план.',
                    'months': engine.seasonal_comparison(row, self.data.data_date)}
        if name == 'simulate_product':
            params = {'codes': [code]}
            for field, target in [('lead_days', 'lead_time_days'), ('review_days', 'review_days'),
                                  ('growth_pct', 'growth_plan_pct'), ('seasonality', 'use_seasonality')]:
                if args[field] is not None:
                    params[target] = args[field]
            overrides = {target: args[field] for field, target in [('stock', 'stock_now'), ('in_transit', 'in_transit')]
                         if args[field] is not None}
            if overrides:
                params['overrides'] = {code: overrides}
            result = engine.calculate(self.data, engine.Params(**params)).iloc[0]
            previous = self.scenario_state.get(code)
            self.scenario_state[code] = {'parameters': args, 'quantity': int(result.qty_recommended)}
            return {'base': self.summary(row), 'scenario': self.summary(result),
                    'quantity_delta': int(result.qty_recommended - row.qty_recommended),
                    'previous_scenario': previous,
                    'quantity_delta_from_previous': int(result.qty_recommended - previous['quantity']) if previous else None,
                    'changed': {k: v for k, v in args.items() if k != 'code' and v is not None},
                    'saved': False, 'baseline': 'base и quantity_delta относятся к стандартному расчёту по файлам, не к предыдущему сценарию диалога.',
                    'note': 'Условный пересчёт, данные не изменены.' + (
                        ' Общее количество в пути заменено; применяется ближайшая дата поступления, а при неизвестной дате — срок поставки.'
                        if args['in_transit'] is not None and args['in_transit'] > 0 else '')}
        raise ValueError('Неизвестный инструмент.')
