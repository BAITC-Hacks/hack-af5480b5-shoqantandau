from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from procurement.views import DemoLoginView

admin.site.site_header = "Заказы поставщикам — администрирование"
admin.site.site_title = "Администрирование"
admin.site.index_title = "Пользователи и роли"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", DemoLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("procurement.urls")),
]
