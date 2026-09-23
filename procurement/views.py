import constants
from django.shortcuts import render


def _data_status():
    """Какие файлы данных лежат на месте — по каждому поставщику."""
    result = []
    for key, sup in constants.SUPPLIERS.items():
        files = []
        for kind, fname in constants.DATA_FILES.items():
            path = sup["dir"] / fname
            files.append({
                "kind": kind,
                "name": fname,
                "exists": path.exists(),
                "size_kb": round(path.stat().st_size / 1024) if path.exists() else 0,
            })
        result.append({"key": key, "name": sup["name"], "files": files,
                       "ready": all(f["exists"] for f in files)})
    return result


def index(request):
    return render(request, "procurement/index.html", {"suppliers": _data_status()})
