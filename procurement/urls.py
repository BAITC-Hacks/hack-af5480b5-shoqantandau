from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.run_form, name="run_form"),
    path("data/", views.index, name="index"),
    path("run/<int:pk>/", views.run_detail, name="run_detail"),
    path("run/<int:pk>/export/", views.export_run, name="export_run"),
    path("check/", views.check_scenarios, name="check"),
    path("backtest/", views.backtest_view, name="backtest"),
    path("line/<int:pk>/explain/", views.explain_line, name="explain_line"),
    path("reload/", views.reload_data, name="reload"),
    path("upload/", views.upload_data, name="upload"),
    path("upload/reset/", views.reset_uploads, name="reset_uploads"),
    path("sku/", views.catalog, name="sku_search"),
    path("line/<int:pk>/reason/", views.line_reason, name="line_reason"),
    path("sku/<str:supplier>/<str:code>/", views.sku_detail, name="sku_detail"),
]
