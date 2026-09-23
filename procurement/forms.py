"""Server-side validation; browser input restrictions are only a convenience."""
from django import forms

import constants


class CalculationForm(forms.Form):
    supplier = forms.ChoiceField(choices=[("", "Все"), *[(k, v["name"]) for k, v in constants.SUPPLIERS.items()]], required=False)
    category = forms.CharField(max_length=64, required=False)
    review_days = forms.IntegerField(min_value=1, max_value=365)
    growth_plan_pct = forms.FloatField(min_value=-100, max_value=300, required=False)
    use_outliers = forms.BooleanField(required=False)
    use_stockout = forms.BooleanField(required=False)
    use_seasonality = forms.BooleanField(required=False)
    use_growth = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key in constants.SUPPLIERS:
            self.fields[f"lead_{key}"] = forms.IntegerField(min_value=1, max_value=365, required=False)

    def clean_growth_plan_pct(self):
        import math
        value = self.cleaned_data["growth_plan_pct"]
        if value is not None and not math.isfinite(value):
            raise forms.ValidationError("Укажите конечное число от −100 до 300.")
        return value
