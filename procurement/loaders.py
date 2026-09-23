"""Загрузка и очистка выгрузок 1С по одному поставщику.

Результат — объект SupplierData с таблицами в едином формате:
  transactions  построчные продажи: date, doc, code, qty (только расходные накладные, qty > 0)
  monthly       SKU x месяц: code, month (Period[M]), sales_1c, stock_open
  items         справочник SKU (index = code): name, article, unit, category, abc_class,
                moq, in_transit, next_arrival, stock_now
  seasonality   продажи в деньгах: index = год, колонки 1..12
  orders        заказы в пути: order_date, arrival_date, lead_days, qty_total

Ключ склейки везде — Код 1С (строка вида '030200519_').
Загрузка больших xlsx медленная, поэтому результат кэшируется в data/cache/*.pkl
и пересчитывается, только если исходные файлы изменились.
"""
from __future__ import annotations

import pickle
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import constants

MONTHS_RU = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
             "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
MONTHS_GEN = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
              "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}

CACHE_VERSION = 9


@dataclass
class SupplierData:
    key: str
    name: str
    transactions: pd.DataFrame
    monthly: pd.DataFrame
    items: pd.DataFrame
    seasonality: pd.DataFrame
    orders: pd.DataFrame
    data_date: date
    warnings: list[str] = field(default_factory=list)
    shipments: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def lead_time_source(self) -> str:
        if constants.SUPPLIERS.get(self.key, {}).get("lead_time_days"):
            return "справочник поставщиков (constants.py)"
        if not self.orders.empty and self.orders["lead_days"].notna().any():
            return "по заказам в пути"
        return "по умолчанию"

    @property
    def lead_time_days(self) -> int:
        """Срок поставки: справочник поставщиков, иначе медиана по заказам в пути, иначе по умолчанию."""
        fixed = constants.SUPPLIERS.get(self.key, {}).get("lead_time_days")
        if fixed:
            return int(fixed)
        o = self.orders
        if not o.empty and o["lead_days"].notna().any():
            # медиана, взвешенная по количеству: крупные поставки важнее мелких довозов
            o = o.dropna(subset=["lead_days"]).sort_values("lead_days")
            w = o["qty_total"].clip(lower=0)
            if w.sum() <= 0:
                return int(o["lead_days"].median())
            return int(o.loc[w.cumsum() >= w.sum() / 2, "lead_days"].iloc[0])
        return constants.DEFAULT_LEAD_TIME_DAYS

    def summary(self) -> dict:
        t = self.transactions
        return {
            "sku": len(self.items),
            "sku_with_sales": int(t["code"].nunique()) if not t.empty else 0,
            "lines": len(t),
            "docs": int(t["doc"].nunique()) if not t.empty else 0,
            "date_from": t["date"].min().date() if not t.empty else None,
            "date_to": t["date"].max().date() if not t.empty else None,
            "in_transit_sku": int((self.items["in_transit"] > 0).sum()),
            "in_transit_qty": float(self.items["in_transit"].sum()),
            "lead_time_days": self.lead_time_days,
            "lead_time_source": self.lead_time_source,
            "marked": int(self.items["marked"].sum()) if "marked" in self.items else 0,
            "data_date": self.data_date,
        }


# ---------- helpers ----------

try:  # calamine (Rust) читает xlsx в ~5 раз быстрее openpyxl; если не установлен — openpyxl
    import python_calamine  # noqa: F401
    EXCEL_ENGINE = "calamine"
except ImportError:
    EXCEL_ENGINE = "openpyxl"


def _read_raw(path: Path) -> pd.DataFrame:
    """Первый лист целиком, без заголовка — файл читается один раз."""
    return pd.read_excel(path, sheet_name=0, header=None, engine=EXCEL_ENGINE)


def _with_header(raw: pd.DataFrame, h: int) -> pd.DataFrame:
    df = raw.iloc[h + 1:].reset_index(drop=True)
    cols, seen = [], {}
    for i, c in enumerate(raw.iloc[h].tolist()):
        name = str(c).strip() if c is not None and not (isinstance(c, float) and np.isnan(c)) else f"Unnamed: {i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    df.columns = cols
    return df

def _norm_code(v) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


def _parse_month_header(h) -> pd.Period | None:
    """'янв. 2024', 'Январь 2024 г.', 'сент. 2026' -> Period('2024-01')."""
    if not isinstance(h, str):
        return None
    m = re.search(r"([А-Яа-яё]+)\.?\s+(\d{4})", h)
    if not m:
        return None
    mon = MONTHS_RU.get(m.group(1)[:3].lower())
    return pd.Period(year=int(m.group(2)), month=mon, freq="M") if mon else None


def _find_header_row(raw: pd.DataFrame, must_contain: str, max_rows: int = 10) -> int:
    for i in range(min(max_rows, len(raw))):
        if raw.iloc[i].astype(str).str.contains(must_contain, regex=False).any():
            return i
    raise ValueError(f"Не найдена строка заголовка с '{must_contain}'")


def _col(df: pd.DataFrame, *variants: str) -> str:
    """Найти колонку по одному из вариантов названия (без учёта регистра и пробелов)."""
    norm = {str(c).strip().lower(): c for c in df.columns}
    for v in variants:
        if v.lower() in norm:
            return norm[v.lower()]
    for v in variants:
        for k, c in norm.items():
            if v.lower() in k:
                return c
    raise KeyError(f"Нет колонки {variants}; есть: {list(df.columns)[:12]}")


def _wide_months_to_long(df: pd.DataFrame, code_col: str, value_name: str) -> pd.DataFrame:
    month_cols = {c: _parse_month_header(c) for c in df.columns}
    month_cols = {c: p for c, p in month_cols.items() if p is not None}
    part = df[[code_col] + list(month_cols)].copy()
    part[code_col] = part[code_col].map(_norm_code)
    part = part.dropna(subset=[code_col])
    long = part.melt(id_vars=code_col, var_name="col", value_name=value_name)
    long["month"] = long["col"].map(month_cols)
    long[value_name] = pd.to_numeric(long[value_name], errors="coerce").fillna(0.0)
    long = long.rename(columns={code_col: "code"})
    return long.groupby(["code", "month"], as_index=False)[value_name].sum()


# ---------- file readers ----------

def read_transactions(path: Path) -> pd.DataFrame:
    df = _with_header(_read_raw(path), 0)
    df = df[df["Дата"].astype(str).str.match(r"\d{2}\.\d{2}\.\d{4}")]
    df = df[df["Документ"].astype(str).str.startswith("Расходная накладная")]
    out = pd.DataFrame({
        "date": pd.to_datetime(df["Дата"], format="%d.%m.%Y %H:%M:%S", errors="coerce"),
        "doc": df["Номер"].astype(str).str.strip(),
        "code": df["Код"].map(_norm_code),
        "name": df["Номенклатура"].astype(str).str.strip(),
        "warehouse": df["Склад"].astype(str).str.strip(),
        # в выгрузке знак количества непоследователен — берём модуль
        "qty": pd.to_numeric(df["Количество"], errors="coerce").abs(),
    })
    # Only explicitly anonymized IDs are accepted. Invoice numbers are not client IDs.
    for col in ("client_id", "Обезличенный ID клиента", "Клиент ID"):
        if col in df.columns:
            out["client_id"] = df[col].map(_norm_code)
            break
    out = out.dropna(subset=["date", "code", "qty"])
    return out[out["qty"] > 0].reset_index(drop=True)


def read_sales_monthly(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = _read_raw(path)
    h = _find_header_row(raw, "Номенклатура.Код")
    df = _with_header(raw, h)
    code = _col(df, "Номенклатура.Код")
    long = _wide_months_to_long(df, code, "sales_1c")
    long["sales_1c"] = long["sales_1c"].clip(lower=0)  # возвраты не делают спрос отрицательным
    ref_cols = {"name": _col(df, "Номенклатура")}
    try:
        ref_cols["article"] = _col(df, "Артикул")
    except KeyError:
        pass
    ref = df[[code] + list(ref_cols.values())].rename(columns={code: "code", **{v: k for k, v in ref_cols.items()}})
    ref["code"] = ref["code"].map(_norm_code)
    return long, ref.dropna(subset=["code"]).drop_duplicates("code")


def read_stock_monthly(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = _read_raw(path)
    h = _find_header_row(raw, "Номенклатура.Код")
    df = _with_header(raw, h)
    code = _col(df, "Номенклатура.Код")
    long = _wide_months_to_long(df, code, "stock_open")  # начальный остаток месяца
    try:
        unit_col = _col(df, "Ед.изм", "Ед.")
        units = df[[code, unit_col]].rename(columns={code: "code", unit_col: "unit"})
        units["code"] = units["code"].map(_norm_code)
        units = units.dropna(subset=["code"]).drop_duplicates("code")
    except KeyError:
        units = pd.DataFrame(columns=["code", "unit"])
    return long, units


def read_moq(path: Path) -> pd.DataFrame:
    raw = _read_raw(path)
    h = _find_header_row(raw, "Код")
    df = _with_header(raw, h)
    code = _col(df, "Код 1с", "Номенклатура.Код")
    q = _col(df, "Мин. разр", "Кратность")
    out = pd.DataFrame({"code": df[code].map(_norm_code),
                        "moq": pd.to_numeric(df[q], errors="coerce")})
    out = out.dropna(subset=["code"])
    out["moq"] = out["moq"].where(out["moq"] >= 1, 1).fillna(1)
    try:
        out["article"] = df[_col(df, "Артикул поставщика", "Артикул")].astype(str).str.strip()
    except KeyError:
        pass
    return out.drop_duplicates("code")


_ORDER_RE = re.compile(r"от\s+(\d{1,2})\s+([а-я]+)\s+(\d{4}).*?до\s+(\d{2})\.(\d{2})\.(\d{4})", re.S)


def read_in_transit(path: Path, data_date: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Два формата:
    IEK — колонка на каждый заказ, в заголовке дата заказа и дата поступления;
    SE  — сводный отчёт закупщика с колонкой 'в пути', категорией и текущими остатками.
    Возвращает (по SKU: in_transit, next_arrival), (заказы), (доп. поля SKU).
    """
    raw = _read_raw(path)
    h = _find_header_row(raw, "Код 1с")
    df = _with_header(raw, h)
    code = _col(df, "Код 1с")
    df[code] = df[code].map(_norm_code)
    df = df.dropna(subset=[code])

    order_cols = {c: _ORDER_RE.search(str(c).replace("\xa0", " ")) for c in df.columns}
    order_cols = {c: m for c, m in order_cols.items() if m}
    orders, per_sku, extra = [], [], pd.DataFrame({"code": df[code]})
    shipments = []

    if order_cols:  # формат IEK
        qty_parts, arrivals = [], []
        for c, m in order_cols.items():
            d0 = date(int(m.group(3)), MONTHS_GEN.get(m.group(2), 1), int(m.group(1)))
            d1 = date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
            q = pd.to_numeric(df[c], errors="coerce").fillna(0)
            orders.append({"order": str(c).split("(")[0].strip(), "order_date": d0, "arrival_date": d1,
                           "lead_days": (d1 - d0).days, "qty_total": float(q.sum())})
            qty_parts.append(q.rename(c))
            shipments.extend({"code": sku, "qty": float(v), "arrival_date": pd.Timestamp(d1)}
                             for sku, v in zip(df[code], q) if v > 0)
            arrivals.append(pd.Series(np.where(q > 0, pd.Timestamp(d1), pd.NaT), index=df.index))
        qty = pd.concat(qty_parts, axis=1).sum(axis=1)
        next_arr = pd.concat(arrivals, axis=1).min(axis=1)
        per_sku = pd.DataFrame({"code": df[code], "in_transit": qty, "next_arrival": next_arr})
    else:  # формат SE
        tcol = next((c for c in df.columns if "в пути" in str(c).lower()), None)
        qty = pd.to_numeric(df[tcol], errors="coerce").fillna(0) if tcol else 0
        arr = pd.NaT
        if tcol:
            m = re.search(r"(\d{2})\.(\d{2})", str(tcol))
            if m:
                arr = pd.Timestamp(data_date.year, int(m.group(2)), int(m.group(1)))
        per_sku = pd.DataFrame({"code": df[code], "in_transit": qty,
                                "next_arrival": np.where(qty > 0, arr, pd.NaT)})
        if tcol:
            shipments.extend({"code": sku, "qty": float(v), "arrival_date": arr}
                             for sku, v in zip(df[code], qty) if v > 0)
        for src, dst in [("Категория 2026", "abc_class"), ("Свободный остаток", "stock_free"), ("СС реал", "cost"),
                         ("Остаток", "stock_total"), ("Артикул поставщика", "article"),
                         ("Наименование", "name")]:
            try:
                extra[dst] = df[_col(df, src)].values
            except KeyError:
                pass

    per_sku = per_sku.groupby("code", as_index=False).agg(in_transit=("in_transit", "sum"),
                                                          next_arrival=("next_arrival", "min"))
    per_sku.attrs["shipments"] = pd.DataFrame(shipments, columns=["code", "qty", "arrival_date"])
    return per_sku, pd.DataFrame(orders), extra.drop_duplicates("code")


def read_seasonality(path: Path) -> pd.DataFrame:
    raw = _read_raw(path)
    h = _find_header_row(raw, "год")
    df = _with_header(raw, h)
    df = df[pd.to_numeric(df["год"], errors="coerce").notna()].copy()
    df["год"] = df["год"].astype(int)
    months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    out = df.set_index("год")[months].apply(pd.to_numeric, errors="coerce")
    out.columns = range(1, 13)
    return out


# ---------- assembly ----------

def _category_from_code(code: str) -> str:
    """Группа номенклатуры 1С — первые 4 цифры кода (у IEK нет явной категории)."""
    digits = re.sub(r"\D", "", code)
    return digits[:4] if len(digits) >= 4 else "прочее"


UPLOAD_DIR = constants.BASE_DIR / "data" / "uploads"


def resolve_files(key: str, directory: Path | None = None) -> dict[str, Path]:
    """Файл, загруженный через интерфейс (data/uploads/<поставщик>/), важнее демо-файла."""
    d = Path(directory or constants.SUPPLIERS[key]["dir"])
    up = UPLOAD_DIR / key
    required = {k: (up / v if (up / v).exists() else d / v) for k, v in constants.DATA_FILES.items()}
    for kind, filename in constants.OPTIONAL_DATA_FILES.items():
        path = up / filename if (up / filename).exists() else d / filename
        if path.exists():
            required[kind] = path
    return required


def read_current_stock(path: Path, data_date: date) -> pd.Series:
    raw = _read_raw(path)
    df = _with_header(raw, _find_header_row(raw, "Код"))
    code, qty, stamp = _col(df, "Код 1с", "Номенклатура.Код"), _col(df, "Свободный остаток"), _col(df, "Дата")
    df[code] = df[code].map(_norm_code)
    df = df.dropna(subset=[code]).copy()
    q = pd.to_numeric(df[qty], errors="coerce")
    dates = pd.to_datetime(df[stamp], dayfirst=True, errors="coerce")
    if df.empty or df[code].duplicated().any() or not np.isfinite(q).all() or (q < 0).any():
        raise ValueError("Текущие остатки: нужны уникальные коды и неотрицательные числа без пустых значений.")
    if dates.isna().any() or not (dates.dt.date == data_date).all():
        raise ValueError(f"Дата текущих остатков должна совпадать с датой расчёта {data_date}.")
    return pd.Series(q.to_numpy(), index=df[code], name="stock_now")


def apply_stockout_periods(monthly: pd.DataFrame, path: Path, data_date: date) -> pd.DataFrame:
    raw = _read_raw(path)
    df = _with_header(raw, _find_header_row(raw, "Код"))
    code, start, end = _col(df, "Код 1с", "Номенклатура.Код"), _col(df, "Начало"), _col(df, "Конец")
    df[code] = df[code].map(_norm_code)
    df = df.dropna(subset=[code]).copy()
    starts = pd.to_datetime(df[start], dayfirst=True, errors="coerce").dt.normalize()
    ends = pd.to_datetime(df[end], dayfirst=True, errors="coerce").dt.normalize()
    if df.empty or starts.isna().any() or ends.isna().any() or (ends < starts).any():
        raise ValueError("Stockout: нужны код товара, корректные даты начала и конца периода.")
    days_by_code = {}
    min_date = monthly["month"].min().start_time
    cutoff = pd.Timestamp(data_date)
    for sku, begin, finish in zip(df[code], starts, ends):
        covered = days_by_code.setdefault(sku, set())
        covered.update(pd.date_range(max(begin, min_date), min(finish, cutoff), freq="D"))
    out = monthly.copy()
    counts = {}
    for sku, dates in days_by_code.items():
        for day in dates:
            key = (sku, day.to_period("M"))
            counts[key] = counts.get(key, 0) + 1
    out["stockout_days"] = [counts.get((sku, month), 0) if sku in days_by_code else np.nan
                           for sku, month in zip(out["code"], out["month"])]
    return out


def load_supplier(key: str, directory: Path | None = None) -> SupplierData:
    sup = constants.SUPPLIERS[key]
    files = resolve_files(key, directory)
    missing = [str(p.name) for p in files.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"{sup['name']}: нет файлов {', '.join(missing)}")

    warnings: list[str] = []
    tx = read_transactions(files["sales_transactions"])
    data_date = tx["date"].max().date() if not tx.empty else date.today()

    sales_m, ref_sales = read_sales_monthly(files["sales_monthly"])
    stock_m, units = read_stock_monthly(files["stock_monthly"])
    moq = read_moq(files["moq"])
    transit, orders, extra = read_in_transit(files["in_transit"], data_date)
    shipments = transit.attrs.get("shipments", pd.DataFrame())
    season = read_seasonality(files["seasonality"])

    monthly = pd.merge(sales_m, stock_m, on=["code", "month"], how="outer").fillna(
        {"sales_1c": 0.0, "stock_open": 0.0})
    if "stockouts" in files:
        monthly = apply_stockout_periods(monthly, files["stockouts"], data_date)

    # справочник SKU: объединение всех источников
    codes = pd.Index(sorted(set(monthly["code"]) | set(tx["code"]) | set(moq["code"]) | set(transit["code"])))
    items = pd.DataFrame(index=codes)
    items.index.name = "code"
    names = pd.concat([
        ref_sales.set_index("code")["name"],
        tx.drop_duplicates("code").set_index("code")["name"],
        extra.set_index("code")["name"] if "name" in extra else pd.Series(dtype=str),
    ])
    items["name"] = names[~names.index.duplicated()].reindex(codes)
    # мусорные строки выгрузок (без наименования, код '0') отбрасываем
    items = items[items["name"].notna() & (items["name"].astype(str).str.strip() != "")]
    items["name"] = items["name"].astype(str).str.strip()
    codes = items.index
    monthly = monthly[monthly["code"].isin(codes)]
    art = pd.concat([moq.set_index("code").get("article", pd.Series(dtype=str)),
                     ref_sales.set_index("code").get("article", pd.Series(dtype=str)),
                     extra.set_index("code").get("article", pd.Series(dtype=str))]).dropna()
    items["article"] = art[~art.index.duplicated()].reindex(codes).fillna("")
    items["unit"] = units.set_index("code")["unit"].reindex(codes).fillna("шт")
    items["category"] = [_category_from_code(c) for c in codes]
    items["marked"] = items["name"].str.contains(constants.DISCONTINUED_MARK, regex=False)
    if "abc_class" in extra:
        abc = pd.to_numeric(extra.set_index("code")["abc_class"], errors="coerce").reindex(codes)
        items["abc_class"] = abc.map(lambda v: "" if pd.isna(v) else str(int(v)))
        provided = items["abc_class"] != ""
        items.loc[provided, "category"] = "SE-" + items.loc[provided, "abc_class"]
    else:
        items["abc_class"] = ""
    items["moq"] = moq.set_index("code")["moq"].reindex(codes).fillna(1)
    if "cost" in extra:  # себестоимость единицы (есть только в отчёте закупщика SE)
        cost = pd.to_numeric(extra.set_index("code")["cost"], errors="coerce").reindex(codes)
        items["cost"] = cost.where(cost > 0)
    else:
        items["cost"] = np.nan
    items["in_transit"] = transit.set_index("code")["in_transit"].reindex(codes).fillna(0)
    items["next_arrival"] = transit.set_index("code")["next_arrival"].reindex(codes)

    # текущий остаток
    if "stock_free" in extra:
        items["stock_now"] = pd.to_numeric(extra.set_index("code")["stock_free"], errors="coerce").reindex(codes)
        stock_source = "свободный остаток из отчёта закупщика"
    else:
        items["stock_now"] = np.nan
        stock_source = "начальный остаток текущего месяца минус продажи с начала месяца по отчёту 1С"
    items["stock_estimated"] = items["stock_now"].isna()
    items["moq_missing"] = ~items.index.isin(moq["code"])
    cur = pd.Period(data_date, freq="M")
    open_cur = stock_m[stock_m["month"] == cur].set_index("code")["stock_open"]
    # продажи текущего месяца — из того же месячного отчёта 1С, что и остатки (накладные с ним расходятся)
    sold_cur = sales_m[sales_m["month"] == cur].groupby("code")["sales_1c"].sum()
    approx = (open_cur.reindex(codes).fillna(0) - sold_cur.reindex(codes).fillna(0)).clip(lower=0)
    items["stock_now"] = items["stock_now"].fillna(approx)
    if "stock_current" in files:
        current = read_current_stock(files["stock_current"], data_date)
        matched = items.index.intersection(current.index)
        items.loc[matched, "stock_now"] = current.loc[matched]
        items.loc[matched, "stock_estimated"] = False
        warnings.append(f"Точная ведомость на {data_date}: остатки обновлены для {len(matched)} позиций.")
    warnings.append(f"Базовый источник текущего остатка: {stock_source}.")
    approximate_count = int(items["stock_estimated"].sum())
    if approximate_count:
        warnings.append(f"У {approximate_count} позиций остаток оценён без поступлений текущего месяца. "
                        "Перед утверждением сверить с актуальной материальной ведомостью 1С.")
    if "client_id" not in tx or not tx["client_id"].notna().any():
        warnings.append("ID клиентов отсутствуют: выбросы выявляются по накладным и месяцам. "
                        "Объединить несколько накладных одного клиента по этим файлам невозможно.")
    warnings.append("Загружены точные периоды stockout; для перечисленных товаров они приоритетнее месячной оценки."
                    if "stockouts" in files else
                    "Отдельные периоды stockout не переданы: отсутствие товара оценивается по месячным остаткам.")

    # сверка построчных продаж с месячным отчётом 1С (с 2024 г.)
    since = pd.Period("2024-01", freq="M")
    tx_total = tx[tx["date"].dt.to_period("M") >= since]["qty"].sum()
    m_total = monthly[monthly["month"] >= since]["sales_1c"].sum()
    if m_total > 0 and abs(tx_total / m_total - 1) > 0.1:
        fmt = lambda v: f"{v:,.0f}".replace(",", " ")
        warnings.append(
            f"Построчные продажи ({fmt(tx_total)}) расходятся с месячным отчётом 1С ({fmt(m_total)}) "
            f"на {abs(tx_total / m_total - 1):.0%}. Спрос берётся из месячного отчёта, "
            f"построчные данные — только для поиска разовых крупных заказов.")

    if not sup.get("lead_time_days") and orders.empty:
        warnings.append(f"Срок поставки не выводится из данных — используется {constants.DEFAULT_LEAD_TIME_DAYS} дн.")
    if sup.get("lead_time_days"):
        warnings.append(f"Срок поставки {sup['lead_time_days']} дн. — из справочника поставщиков (constants.py).")
    n_marked = int(items["marked"].sum())
    if n_marked:
        warnings.append(f"{n_marked} арт. с пометкой «{constants.DISCONTINUED_MARK}» в названии — похоже на вывод из "
                        f"ассортимента: упущенный спрос для них не восстанавливается, позиции помечаются «проверить».")
    if tx.empty:
        warnings.append("Нет построчных продаж — разовые заказы не выявляются.")

    return SupplierData(key=key, name=sup["name"], transactions=tx, monthly=monthly, items=items,
                        seasonality=season, orders=orders, data_date=data_date, warnings=warnings, shipments=shipments)


# ---------- cache ----------

CACHE_DIR = constants.BASE_DIR / "data" / "cache"
_MEMORY: dict = {}  # данные в памяти процесса: страницы не перечитывают кэш с диска
import threading  # noqa: E402

_LOCK = threading.Lock()


def is_ready() -> bool:
    return all(k in _MEMORY for k in constants.SUPPLIERS)


def _fingerprint(files: dict[str, Path]) -> tuple:
    return tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files.values() if f.exists())


def get_supplier(key: str, directory: Path | None = None, use_cache: bool = True) -> SupplierData:
    d = Path(directory or constants.SUPPLIERS[key]["dir"])
    cache = CACHE_DIR / f"{key}.pkl"
    fp = (CACHE_VERSION, _fingerprint(resolve_files(key, directory)))
    mem = _MEMORY.get(key)
    if use_cache and mem and mem[0] == fp:
        return mem[1]
    if use_cache and cache.exists():
        try:
            with open(cache, "rb") as fh:
                saved_fp, data = pickle.load(fh)
            if saved_fp == fp:
                _MEMORY[key] = (fp, data)
                return data
        except Exception:
            pass
    with _LOCK:  # два запроса одновременно не разбирают одни и те же файлы
        mem = _MEMORY.get(key)
        if use_cache and mem and mem[0] == fp:
            return mem[1]
        data = load_supplier(key, d)
        _MEMORY[key] = (fp, data)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as fh:
        pickle.dump((fp, data), fh)
    return data


def get_all(use_cache: bool = True) -> dict[str, SupplierData]:
    return {k: get_supplier(k, use_cache=use_cache) for k in constants.SUPPLIERS}
