from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.run_form, name="run_form"),
    path("data/", views.index, name="index"),
    path("run/<int:pk>/", views.run_detail, name="run_detail"),
    path("reload/", views.reload_data, name="reload"),
    path("sku/", views.sku_search, name="sku_search"),
    path("sku/<str:supplier>/<str:code>/", views.sku_detail, name="sku_detail"),
]
