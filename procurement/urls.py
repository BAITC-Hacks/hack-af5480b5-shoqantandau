from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.index, name="index"),
    path("reload/", views.reload_data, name="reload"),
    path("sku/", views.sku_search, name="sku_search"),
    path("sku/<str:supplier>/<str:code>/", views.sku_detail, name="sku_detail"),
]
