import json
import constants
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from . import agent, agent_config
from .views import _need


def page(request):
    return render(request, 'procurement/agent.html', {
        'suppliers': constants.SUPPLIERS, 'enabled': bool(agent_config.api_key()),
        'model': agent_config.MODEL, 'budget': agent.budget_status(),
        'initial_history': request.session.get('agent_history', []),
        'initial_supplier': request.session.get('agent_supplier', 'techno'),
        'can_ask': request.user.has_perm('procurement.edit_order')})


@require_POST
def ask(request):
    _need(request, 'edit_order')
    if len(request.body) > 8000:
        return JsonResponse({'error': 'Сообщение слишком большое.'}, status=400)
    try:
        body = json.loads(request.body)
        supplier, question = body.get('supplier'), body.get('message')
        if supplier not in constants.SUPPLIERS or not isinstance(question, str) or not 1 <= len(question.strip()) <= 1500:
            raise ValueError()
        question = question.strip()
    except (ValueError, AttributeError, TypeError):
        return JsonResponse({'error': 'Выберите поставщика и введите вопрос до 1500 символов.'}, status=400)
    lock = f'agent-user-{request.user.pk}'
    if not cache.add(lock, True, timeout=180):
        return JsonResponse({'error': 'Предыдущий запрос ещё выполняется.'}, status=429)
    try:
        history = request.session.get('agent_history', []) if request.session.get('agent_supplier') == supplier else []
        previous = request.session.get('agent_scenarios', {}) if request.session.get('agent_supplier') == supplier else {}
        result = agent.answer(request.user, supplier, question, history, previous)
        request.session['agent_scenarios'] = result.pop('scenario_state', {})
        request.session['agent_supplier'] = supplier
        request.session['agent_history'] = (history + [{'role': 'user', 'content': question},
                                                       {'role': 'assistant', 'content': result['answer'][:3500]}])[-6:]
        return JsonResponse(result)
    except agent.AgentError as error:
        return JsonResponse({'error': str(error), 'budget': agent.budget_status()}, status=503)
    except (ValueError, KeyError, TypeError, FileNotFoundError):
        return JsonResponse({'error': 'Не удалось обработать данные. Проверьте файлы и уточните запрос.',
                             'budget': agent.budget_status()}, status=503)
    finally:
        cache.delete(lock)


@require_POST
def reset(request):
    _need(request, 'edit_order')
    request.session.pop('agent_history', None)
    request.session.pop('agent_supplier', None)
    request.session.pop('agent_scenarios', None)
    return JsonResponse({'ok': True})
