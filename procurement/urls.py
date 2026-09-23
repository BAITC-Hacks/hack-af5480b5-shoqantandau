from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.run_form, name="run_form"),
    path("data/", views.index, name="index"),
    path("run/<int:pk>/", views.run_detail, name="run_detail"),
    path("run/<int:pk>/export/", views.export_run, name="export_run"),
    path("check/", views.check_scenarios, name="check"),
    path("line/<int:pk>/explain/", views.explain_line, name="explain_line"),
    path("reload/", views.reload_data, name="reload"),
    path("sku/", views.sku_search, name="sku_search"),
    path("sku/<str:supplier>/<str:code>/", views.sku_detail, name="sku_detail"),
]
