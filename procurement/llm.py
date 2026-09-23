"""Понятное объяснение рекомендации для менеджера.

Расчёт количества делает только engine.py (детерминированно). Языковая модель лишь пересказывает
уже посчитанные цифры простыми словами. Если ключа нет или API недоступен — используется шаблон,
сервис работает полностью.

В модель уходят только агрегаты по артикулу (спрос, остаток, в пути, номера исключённых накладных).
Данные о клиентах не передаются.

Поддерживаются OpenAI и NVIDIA API (оба OpenAI-совместимые, вызов через стандартную библиотеку).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import constants

SYSTEM_PROMPT = (
    "Ты помощник менеджера отдела закупа электротоваров. Тебе дают результат расчёта заказа поставщику "
    "по одному артикулу. Объясни менеджеру простыми словами, почему предложено именно такое количество: "
    "2–4 коротких предложения на русском, без markdown и списков. Используй только цифры из входных данных, "
    "ничего не придумывай и не пересчитывай. Если были исключены разовые крупные продажи или восстановлен "
    "спрос после дефицита — обязательно скажи об этом и чем это грозило бы без учёта. "
    "Если заказывать не нужно — так и скажи и объясни почему."
)


def is_enabled() -> bool:
    if constants.LLM_PROVIDER == "nvidia":
        return bool(constants.NVIDIA_API_KEY)
    return bool(constants.OPENAI_API_KEY)


def provider_label() -> str:
    return "NVIDIA" if constants.LLM_PROVIDER == "nvidia" else "OpenAI"


def _payload(line) -> dict:
    d = line.details or {}
    return {
        "артикул": line.name, "код_1с": line.code_1c,
        "рекомендовано": line.qty_recommended, "к_заказу_после_правки": line.qty_to_order,
        "ед": d.get("unit", "шт"), "остаток": round(line.stock), "в_пути": round(line.in_transit),
        "спрос_в_месяц": round(line.regular_demand, 1), "прогноз_на_горизонт": round(line.forecast),
        "страховой_запас": round(line.safety_stock), "кратность": line.moq,
        "срок_поставки_дн": d.get("lead"), "горизонт_дн": d.get("horizon"),
        "срочность": line.get_urgency_display(),
        "дней_хватит_остатка": None if (line.days_of_cover or 0) >= 9999 else round(line.days_of_cover or 0),
        "исключённые_разовые_продажи": [{"накладная": o["doc"], "дата": o["date"], "кол_во": o["qty"]}
                                         for o in d.get("one_offs", []) if o.get("in_window")],
        "месяцы_без_товара": d.get("stockout_months", []),
        "сглаженные_всплески_месяцев": [{"месяц": x["label"], "было": round(x["was"]), "стало": round(x["now"])}
                                        for x in d.get("spikes", []) if x.get("in_window")],
        "требует_проверки": d.get("flags", []),
        "рекомендация_без_очистки_данных": d.get("qty_raw"),
        "техническое_обоснование": line.reason,
    }


def template_explanation(line) -> str:
    """Запасной вариант без AI: короткий человеческий текст по тем же цифрам."""
    d = line.details or {}
    unit = d.get("unit", "шт")
    parts = []
    if d.get("flags"):
        parts.append("Проверьте позицию: " + "; ".join(d["flags"]) + ".")
    if line.qty_to_order == 0 and "Расчётно нужно" in (line.reason or ""):
        parts.append("Автоматически заказ не предлагается из-за пометки в названии, хотя расчёт показывает потребность — "
                     "если позиция ещё закупается, укажите количество вручную.")
    elif line.qty_to_order > 0:
        cover = "" if (line.days_of_cover or 0) >= 9999 else f" Текущего остатка хватит примерно на {line.days_of_cover:.0f} дн."
        parts.append(f"Предлагаем заказать {line.qty_to_order} {unit}: в среднем уходит {line.regular_demand:.1f} {unit} "
                     f"в месяц, а до следующей поставки и следующего заказа нужно около {line.forecast + line.safety_stock:.0f} {unit} "
                     f"с запасом.{cover}")
    else:
        parts.append(f"Заказывать сейчас не нужно: остатка {line.stock:.0f} и товара в пути {line.in_transit:.0f} "
                     f"хватает на прогноз {line.forecast:.0f} {unit} с запасом.")
    oo = [o for o in d.get("one_offs", []) if o.get("in_window")]
    if oo:
        big = max(oo, key=lambda o: o["qty"])
        parts.append(f"Разовая крупная продажа ({big['qty']:.0f} {unit}, накладная {big['doc']} от {big['date']}) "
                     f"не считается регулярным спросом.")
    sp = [x for x in d.get("spikes", []) if x.get("in_window")]
    if sp:
        parts.append(f"Нетипичный всплеск продаж ({', '.join(x['label'] for x in sp[:3])}) сглажен до обычного уровня.")
    if d.get("stockout_months"):
        parts.append(f"Товара не было на складе ({', '.join(d['stockout_months'])}) — продажи тогда были занижены, "
                     f"поэтому спрос за эти месяцы восстановлен.")
    raw = d.get("qty_raw")
    if raw is not None and raw != line.qty_recommended and (oo or sp or d.get("stockout_months")):
        parts.append(f"Без этих поправок расчёт дал бы {raw} {unit}.")
    return " ".join(parts)


def _call(messages: list[dict]) -> str:
    if constants.LLM_PROVIDER == "nvidia":
        url = constants.NVIDIA_BASE_URL.rstrip("/") + "/chat/completions"
        key, body = constants.NVIDIA_API_KEY, {"model": constants.NVIDIA_MODEL, "messages": messages,
                                               "max_tokens": 400, "temperature": 0.2}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        key, body = constants.OPENAI_API_KEY, {"model": constants.OPENAI_MODEL, "messages": messages,
                                               "max_completion_tokens": 2000}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode())
    text = (data["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise ValueError("пустой ответ модели")
    return text


def explain(line) -> tuple[str, str]:
    """Возвращает (текст, источник). Источник: 'AI (OpenAI/NVIDIA)' или 'шаблон'."""
    if not is_enabled():
        return template_explanation(line), "шаблон (ключ AI не задан)"
    try:
        text = _call([{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(_payload(line), ensure_ascii=False)}])
        return text, f"AI ({provider_label()})"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError) as exc:
        detail = exc.read().decode()[:200] if isinstance(exc, urllib.error.HTTPError) else str(exc)[:200]
        return template_explanation(line), f"шаблон (AI недоступен: {detail})"
