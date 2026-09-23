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
    return render(request, "procurement/sku.html", {
        "supplier": data.name, "code": code, "item": item,
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


def run_detail(request, pk):
    run = get_object_or_404(CalculationRun, pk=pk)
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
    if q:
        from django.db.models import Q
        lines = lines.filter(Q(code_1c__icontains=q) | Q(name__icontains=q) | Q(article__icontains=q))
    order = {"high": 0, "medium": 1, "low": 2}
    lines = sorted(lines, key=lambda x: (order.get(x.urgency, 3), x.days_of_cover or 0))
    page = Paginator(lines, 100).get_page(request.GET.get("page"))
    return render(request, "procurement/run_detail.html", {
        "run": run, "active": active, "tabs": [(k, run.stats.get(k, {})) for k in sup_keys],
        "stat": run.stats.get(active, {}), "page": page, "show": show, "urgency": urgency, "q": q,
    })
