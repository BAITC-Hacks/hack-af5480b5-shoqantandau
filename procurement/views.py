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
