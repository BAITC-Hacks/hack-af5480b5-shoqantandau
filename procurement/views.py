import shutil

import pandas as pd
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

import constants

from . import loaders


def _data_status():
    """Какие файлы данных лежат на месте — по каждому поставщику."""
    result = []
    for key, sup in constants.SUPPLIERS.items():
        files = []
        for kind, fname in constants.DATA_FILES.items():
            path = sup["dir"] / fname
            files.append({"kind": kind, "name": fname, "exists": path.exists(),
                          "size_kb": round(path.stat().st_size / 1024) if path.exists() else 0})
        result.append({"key": key, "name": sup["name"], "files": files,
                       "ready": all(f["exists"] for f in files)})
    return result


def index(request):
    suppliers = _data_status()
    for s in suppliers:
        if not s["ready"]:
            continue
        try:
            data = loaders.get_supplier(s["key"])
            s["summary"] = data.summary()
            s["warnings"] = data.warnings
        except Exception as exc:  # показываем ошибку разбора файла, а не падаем
            s["error"] = f"{type(exc).__name__}: {exc}"
    return render(request, "procurement/index.html", {"suppliers": suppliers})


@require_POST
def reload_data(request):
    shutil.rmtree(loaders.CACHE_DIR, ignore_errors=True)
    messages.info(request, "Данные перечитаны из файлов.")
    return redirect("procurement:index")


def sku_detail(request, supplier, code):
    if supplier not in constants.SUPPLIERS:
        raise Http404("Неизвестный поставщик")
    data = loaders.get_supplier(supplier)
    if code not in data.items.index:
        raise Http404("Артикул не найден")
    item = data.items.loc[code]

    tx = data.transactions[data.transactions["code"] == code]
    tx_m = tx.groupby(tx["date"].dt.to_period("M"))["qty"].sum()
    m = data.monthly[data.monthly["code"] == code].set_index("month").sort_index()
    rows = [{"month": p.strftime("%m.%Y"), "sales_1c": r.sales_1c, "tx": float(tx_m.get(p, 0)),
             "stock_open": r.stock_open} for p, r in m.iterrows()]

    top_docs = (tx.groupby(["doc", tx["date"].dt.date])["qty"].sum()
                .sort_values(ascending=False).head(10).reset_index())
    top_docs.columns = ["doc", "date", "qty"]

    # график: сырые продажи против очищенного спроса (тот же расчёт, что и в заказе)
    from . import charts, engine as eng
    chart, calc = "", None
    res = eng.calculate(data, eng.Params(codes=[code]))
    if not res.empty:
        calc = res.iloc[0]
        det = calc["details"]
        months = det["months"]
        so = {months.index(f"{y}-{m:02d}") for y, m in
              [(int(x.split()[1]), eng.MONTH_NAMES.index(x.split()[0]) + 1) for x in det["stockout_months"]]
              if f"{y}-{m:02d}" in months}
        oo = {months.index(o["month"]) for o in det["one_offs"] if o["month"] in months}
        chart = charts.demand_chart(months, det["raw"], det["clean"], so, oo)
    return render(request, "procurement/sku.html", {
        "chart": chart, "calc": calc,
        "supplier": data.name, "supplier_key": supplier, "code": code, "item": item,
        "rows": rows, "top_docs": top_docs.to_dict("records"),
        "next_arrival": None if pd.isna(item["next_arrival"]) else pd.Timestamp(item["next_arrival"]).date(),
    })


def sku_search(request):
    q = request.GET.get("q", "").strip()
    results = []
    if q:
        for key in constants.SUPPLIERS:
            items = loaders.get_supplier(key).items
            mask = (items.index.str.contains(q, case=False, regex=False)
                    | items["name"].str.contains(q, case=False, regex=False)
                    | items["article"].astype(str).str.contains(q, case=False, regex=False))
            for code, r in items[mask].head(50).iterrows():
                results.append({"supplier_key": key, "supplier": constants.SUPPLIERS[key]["name"],
                                "code": code, "name": r["name"], "article": r["article"]})
    return render(request, "procurement/search.html", {"q": q, "results": results})


# ---------- расчёт ----------

from django.core.paginator import Paginator  # noqa: E402
from django.shortcuts import get_object_or_404  # noqa: E402

from . import engine, services  # noqa: E402
from .models import CalculationRun  # noqa: E402


def _num(v, cast=float):
    try:
        return cast(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def run_form(request):
    if request.method == "POST":
        f = request.POST
        params = engine.Params(
            lead_time_days=_num(f.get("lead_time_days"), int),
            review_days=_num(f.get("review_days"), int) or constants.REVIEW_PERIOD_DAYS,
            growth_plan_pct=_num(f.get("growth_plan_pct")),
            category=f.get("category", "").strip(),
            use_outliers=bool(f.get("use_outliers")),
            use_stockout=bool(f.get("use_stockout")),
            use_seasonality=bool(f.get("use_seasonality")),
            use_growth=bool(f.get("use_growth")),
        )
        sup = f.get("supplier") or ""
        run = services.run_calculation(params, [sup] if sup in constants.SUPPLIERS else None)
        return redirect("procurement:run_detail", run.pk)
    return render(request, "procurement/run_form.html", {
        "suppliers": constants.SUPPLIERS, "review_days": constants.REVIEW_PERIOD_DAYS,
        "runs": CalculationRun.objects.all()[:10],
    })


def _apply_edits(request, run):
    """Сохранить правки количества и/или изменить статус выбранных строк."""
    action = request.POST.get("action", "save")
    ids = [int(x) for x in request.POST.getlist("sel") if x.isdigit()]
    changed = 0
    for key, val in request.POST.items():
        if not key.startswith("qty_"):
            continue
        line_id = _num(key[4:], int)
        new = _num(val, int)
        if line_id is None:
            continue
        line = run.lines.filter(pk=line_id).first()
        if line is None:
            continue
        final = None if new is None or new == line.qty_recommended else max(new, 0)
        if final != line.qty_final:
            line.qty_final = final
            line.save(update_fields=["qty_final"])
            changed += 1
    if action in ("approve", "reject", "reset") and ids:
        status = {"approve": "approved", "reject": "rejected", "reset": "new"}[action]
        n = run.lines.filter(pk__in=ids).update(status=status)
        label = {"approve": "Утверждено", "reject": "Отклонено", "reset": "Возвращено в работу"}[action]
        messages.success(request, f"{label} позиций: {n}.")
    elif action in ("approve", "reject", "reset"):
        messages.error(request, "Отметьте позиции галочками, затем нажмите кнопку.")
    elif changed:
        messages.success(request, f"Сохранено изменений количества: {changed}.")


def run_detail(request, pk):
    run = get_object_or_404(CalculationRun, pk=pk)
    if request.method == "POST":
        _apply_edits(request, run)
        return redirect(request.get_full_path())
    sup_keys = [k for k in run.supplier.split(",") if k]
    active = request.GET.get("supplier") or (sup_keys[0] if sup_keys else "")
    show = request.GET.get("show", "order")
    urgency = request.GET.get("urgency", "")
    q = request.GET.get("q", "").strip()

    lines = run.lines.filter(supplier=active)
    if show == "order":
        lines = lines.filter(qty_recommended__gt=0)
    if urgency:
        lines = lines.filter(urgency=urgency)
    status = request.GET.get("status", "")
    if status:
        lines = lines.filter(status=status)
    if q:
        from django.db.models import Q
        lines = lines.filter(Q(code_1c__icontains=q) | Q(name__icontains=q) | Q(article__icontains=q))
    order = {"high": 0, "medium": 1, "low": 2}
    lines = sorted(lines, key=lambda x: (order.get(x.urgency, 3), x.days_of_cover or 0))
    page = Paginator(lines, 100).get_page(request.GET.get("page"))
    return render(request, "procurement/run_detail.html", {
        "run": run, "active": active, "tabs": [(k, run.stats.get(k, {})) for k in sup_keys],
        "stat": run.stats.get(active, {}), "page": page, "show": show, "urgency": urgency, "q": q,
        "status": status, "approved": run.lines.filter(supplier=active, status="approved").count(),
        "ai_label": llm.provider_label() if llm.is_enabled() else "",
    })


# ---------- экспорт ----------

import io  # noqa: E402

from django.http import HttpResponse  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font  # noqa: E402


def export_run(request, pk):
    """Выгрузка в xlsx: лист на каждого поставщика, по умолчанию только утверждённые позиции."""
    run = get_object_or_404(CalculationRun, pk=pk)
    scope = request.GET.get("scope", "approved")
    only = request.GET.get("supplier")
    wb = Workbook()
    wb.remove(wb.active)
    headers = ["Код 1С", "Артикул поставщика", "Наименование", "Количество", "Ед.", "Кратность",
               "Срочность", "Остаток", "В пути", "Рекомендовано системой", "Обоснование"]
    total = 0
    for key in [k for k in run.supplier.split(",") if k and (not only or k == only)]:
        qs = run.lines.filter(supplier=key)
        qs = qs.filter(status="approved") if scope == "approved" else qs.filter(qty_recommended__gt=0).exclude(status="rejected")
        ws = wb.create_sheet(constants.SUPPLIERS.get(key, {}).get("name", key)[:31])
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for l in qs.order_by("code_1c"):
            if l.qty_to_order <= 0:
                continue
            ws.append([l.code_1c, l.article, l.name, l.qty_to_order, (l.details or {}).get("unit", "шт"),
                       l.moq, l.get_urgency_display(), round(l.stock), round(l.in_transit),
                       l.qty_recommended, l.reason])
            total += 1
        for col, w in zip("ABCDEFGHIJK", [14, 22, 60, 12, 6, 10, 11, 10, 10, 14, 100]):
            ws.column_dimensions[col].width = w
        for row in ws.iter_rows(min_row=2, min_col=11, max_col=11):
            row[0].alignment = Alignment(wrap_text=False)
        ws.freeze_panes = "A2"
    if total == 0 and scope == "approved":
        messages.error(request, "Нет утверждённых позиций. Отметьте позиции и нажмите «Утвердить», "
                                "или выгрузите черновик со всеми рекомендациями.")
        return redirect("procurement:run_detail", pk)
    buf = io.BytesIO()
    wb.save(buf)
    name = f"zakaz_{pk}_{'utverzhden' if scope == 'approved' else 'chernovik'}.xlsx"
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# ---------- проверка для экспертов ----------

DEMO_CODES = {"iek": "030201020_", "se": "300200294_"}  # артикулы для демонстрации по умолчанию


def check_scenarios(request):
    """Один артикул в нескольких сценариях: видно, как каждый источник данных влияет на результат."""
    g = request.GET
    sup = g.get("supplier", "iek") if g.get("supplier") in constants.SUPPLIERS else "iek"
    code = (g.get("code") or DEMO_CODES.get(sup, "")).strip()
    data = loaders.get_supplier(sup)
    ctx = {"suppliers": constants.SUPPLIERS, "sup": sup, "code": code, "g": g}
    if code not in data.items.index:
        ctx["error"] = f"Артикул {code} не найден у поставщика {data.name}."
        return render(request, "procurement/check.html", ctx)

    base_p = dict(codes=[code])
    base = engine.calculate(data, engine.Params(**base_p))
    if base.empty:
        ctx["error"] = "У артикула нет продаж за последние 12 месяцев — заказ не рассчитывается."
        return render(request, "procurement/check.html", ctx)
    b = base.iloc[0]
    item = data.items.loc[code]
    big_qty = _num(g.get("big_qty")) or max(round(b["regular_demand"] * 10), 50)
    transit_add = _num(g.get("transit_add")) or max(round(b["qty_recommended"] / 2), 10)
    stock_val = _num(g.get("stock_val"))
    stock_val = stock_val if stock_val is not None else float(item["stock_now"]) + transit_add
    growth = _num(g.get("growth")) if g.get("growth") not in (None, "") else 30.0
    last_month = (pd.Period(data.data_date, freq="M") - 2).start_time + pd.Timedelta(days=10)
    test_order = [{"code": code, "qty": big_qty, "date": str(last_month.date()), "doc": "ТЕСТ-РАЗОВЫЙ"}]

    scen = [
        ("base", "Базовый расчёт", "Все данные как есть", {}),
        ("oneoff", f"+ разовая продажа {big_qty:.0f} шт.", "Требование 4: заказ исключается, рекомендация почти не меняется",
         {"test_orders": test_order}),
        ("oneoff_raw", f"+ разовая продажа {big_qty:.0f} шт., без очистки", "Для сравнения: так посчитал бы Excel по сырым продажам",
         {"test_orders": test_order, "use_outliers": False}),
        ("transit", f"+ {transit_add:.0f} в пути", "Требование 1: товар в пути уменьшает заказ",
         {"overrides": {code: {"in_transit": float(item["in_transit"]) + transit_add}}}),
        ("stock", f"Остаток = {stock_val:.0f}", "Требование 1: остаток уменьшает заказ",
         {"overrides": {code: {"stock_now": stock_val}}}),
        ("growth", f"Плановый прирост {growth:+.0f}% г/г", "Требование 1: прогноз по приросту", {"growth_plan_pct": growth}),
        ("no_stockout", "Без учёта дефицита", "Требование 3: без восстановления упущенного спроса", {"use_stockout": False}),
        ("no_season", "Без сезонности", "Требование 2: сезонный индекс против среднего", {"use_seasonality": False}),
    ]
    rows = []
    for key, title, hint, extra in scen:
        r = b if key == "base" else engine.calculate(data, engine.Params(**base_p, **extra)).iloc[0]
        rows.append({"key": key, "title": title, "hint": hint, "qty": int(r["qty_recommended"]),
                     "delta": int(r["qty_recommended"]) - int(b["qty_recommended"]),
                     "demand": r["regular_demand"], "forecast": r["forecast"], "reason": r["reason"]})
    ctx.update({"rows": rows, "item": item, "base": b, "big_qty": big_qty, "transit_add": transit_add,
                "stock_val": stock_val, "growth": growth, "supplier_name": data.name})
    return render(request, "procurement/check.html", ctx)


# ---------- объяснение простыми словами ----------

from django.http import JsonResponse  # noqa: E402

from . import llm  # noqa: E402
from .models import OrderLine  # noqa: E402


@require_POST
def explain_line(request, pk):
    line = get_object_or_404(OrderLine, pk=pk)
    text, source = llm.explain(line)
    line.reason_ai = text
    line.save(update_fields=["reason_ai"])
    return JsonResponse({"text": text, "source": source})
