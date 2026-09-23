from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'procurement'

    def ready(self):
        """При запуске сервера данные разбираются в фоне — первая страница открывается сразу."""
        import os
        import sys
        import threading

        if "runserver" in sys.argv and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv):
            from . import loaders

            def warm():
                try:
                    loaders.get_all()
                except Exception:
                    pass

            threading.Thread(target=warm, daemon=True).start()
