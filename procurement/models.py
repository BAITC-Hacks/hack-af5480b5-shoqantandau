from django.db import models


class CalculationRun(models.Model):
    """Один запуск расчёта рекомендованных заказов."""

    created_at = models.DateTimeField(auto_now_add=True)
    supplier = models.CharField("Поставщик", max_length=32, blank=True)  # пусто = все
    category = models.CharField("Категория", max_length=64, blank=True)
    params = models.JSONField("Параметры расчёта", default=dict)
    stats = models.JSONField("Сводка", default=dict)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Расчёт #{self.pk} от {self.created_at:%d.%m.%Y %H:%M}"


class OrderLine(models.Model):
    """Строка рекомендованного заказа по одному артикулу."""

    URGENCY_CHOICES = [("high", "Высокая"), ("medium", "Средняя"), ("low", "Низкая")]
    STATUS_CHOICES = [("new", "Новая"), ("approved", "Утверждена"), ("rejected", "Отклонена")]

    run = models.ForeignKey(CalculationRun, on_delete=models.CASCADE, related_name="lines")
    supplier = models.CharField("Поставщик", max_length=32)
    code_1c = models.CharField("Код 1С", max_length=32)
    article = models.CharField("Артикул поставщика", max_length=64, blank=True)
    name = models.CharField("Наименование", max_length=512)
    category = models.CharField("Категория", max_length=64, blank=True)

    stock = models.FloatField("Остаток", default=0)
    in_transit = models.FloatField("В пути", default=0)
    regular_demand = models.FloatField("Регулярный спрос, шт/мес", default=0)
    forecast = models.FloatField("Прогноз на горизонт", default=0)
    safety_stock = models.FloatField("Страховой запас", default=0)
    moq = models.FloatField("Кратность", default=1)

    qty_recommended = models.IntegerField("Рекомендовано", default=0)
    qty_final = models.IntegerField("К заказу", null=True, blank=True)
    urgency = models.CharField("Срочность", max_length=8, choices=URGENCY_CHOICES, default="low")
    days_of_cover = models.FloatField("Дней покрытия", null=True, blank=True)
    reason = models.TextField("Обоснование", blank=True)
    reason_ai = models.TextField("Обоснование (AI)", blank=True)
    details = models.JSONField("Детали расчёта", default=dict)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default="new")
    needs_review = models.BooleanField("Требует проверки", default=False)
    cost = models.FloatField("Себестоимость единицы", null=True, blank=True)

    class Meta:
        ordering = ["supplier", "-days_of_cover"]
        indexes = [models.Index(fields=["run", "supplier"])]

    def __str__(self):
        return f"{self.code_1c} {self.name[:40]}"

    @property
    def qty_to_order(self):
        return self.qty_final if self.qty_final is not None else self.qty_recommended

    @property
    def value_to_order(self):
        return self.cost * self.qty_to_order if self.cost else None
