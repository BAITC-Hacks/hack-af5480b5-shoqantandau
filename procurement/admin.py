from django.contrib import admin

from .models import CalculationRun, OrderLine


@admin.register(CalculationRun)
class CalculationRunAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "supplier", "category")


@admin.register(OrderLine)
class OrderLineAdmin(admin.ModelAdmin):
    list_display = ("code_1c", "name", "supplier", "qty_recommended", "qty_final", "urgency", "status")
    list_filter = ("supplier", "urgency", "status")
    search_fields = ("code_1c", "article", "name")
