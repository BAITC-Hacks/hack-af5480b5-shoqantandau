"""Exercise the real data pipeline without creating or approving orders."""
import json
import time

import numpy as np
from django.core.management.base import BaseCommand, CommandError

import constants
from procurement import backtest, engine, loaders


class Command(BaseCommand):
    help = "Проверить выгрузки и расчёт по всем подключённым поставщикам, включая синтетические данные."

    def add_arguments(self, parser):
        parser.add_argument("--backtest", action="store_true", help="Также пересчитать проверку прогноза на истории")

    def handle(self, *args, **options):
        report = {}
        started = time.perf_counter()
        for key in constants.SUPPLIERS:
            data = loaders.get_supplier(key)
            frame = engine.calculate(data)
            if frame.empty or not np.isfinite(frame[["qty_recommended", "forecast", "stock", "in_transit"]].to_numpy()).all():
                raise CommandError(f"{key}: пустой или некорректный результат")
            if (frame.qty_recommended < 0).any() or not np.allclose(frame.qty_recommended % frame.moq, 0):
                raise CommandError(f"{key}: нарушение количества или кратности")
            report[key] = {"sku": len(frame), "to_order": int((frame.qty_recommended > 0).sum()),
                           "urgent": int((frame.urgency == "high").sum()),
                           "date": str(data.data_date), "warnings": data.warnings}
        report["calculation_seconds"] = round(time.perf_counter() - started, 2)
        if options["backtest"]:
            result = backtest.run()
            report["backtest"] = {key: value["summary"] for key, value in result["suppliers"].items()}
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
