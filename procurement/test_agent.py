import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase
from . import agent, agent_config, agent_tools
from .models import AgentBudget, AgentCall, CalculationRun, OrderLine


class AgentTools(SimpleTestCase):
    def setUp(self):
        self.context = agent_tools.Context('techno')

    def test_no_mutation_tool_available(self):
        with self.assertRaises(ValueError):
            self.context.call('approve_order', {'id': 1})

    def test_search_is_scoped_and_does_not_return_client_ids(self):
        result=self.context.call('find_products', {'query':'вентилятор'})
        self.assertEqual(result['products'][0]['code'],'1101001_')
        self.assertNotIn('client_id',json.dumps(result))
        with self.assertRaises(ValueError):
            self.context.call('get_product', {'code':'030201020_'})

    def test_arrival_gap_is_visible(self):
        result=self.context.call('get_product', {'code':'3301004_'})['product']
        self.assertEqual(result['recommended_quantity'],0)
        self.assertEqual(result['urgency'],'high')
        self.assertEqual(result['first_shortage_day'],0)

    def test_scenario_uses_engine_and_does_not_modify_stock(self):
        before=self.context.data.items.loc['3301002_', 'stock_now']
        args=dict(code='3301002_',lead_days=30,review_days=None,growth_pct=None,stock=None,in_transit=None,seasonality=None)
        result=self.context.call('simulate_product',args)
        self.assertGreater(result['scenario']['recommended_quantity'], result['base']['recommended_quantity'])
        self.assertFalse(result['saved'])
        self.assertEqual(self.context.data.items.loc['3301002_', 'stock_now'],before)
        args['stock']=100
        followup=self.context.call('simulate_product',args)
        self.assertEqual(followup['previous_scenario']['quantity'],228)
        self.assertEqual(followup['scenario']['recommended_quantity'],132)
        self.assertEqual(followup['quantity_delta_from_previous'],-96)
        self.assertEqual(followup['quantity_delta'],-60)

    def test_seasons_and_invalid_values(self):
        result=self.context.call('compare_seasons', {'code':'1101001_'})
        self.assertGreater(result['months'][6]['qty'],result['months'][0]['qty'])
        with self.assertRaises(ValueError):
            agent_tools.validate('get_product', {'code':'3301002_', 'supplier':'iek'})
        with self.assertRaises(ValueError):
            self.context.call('simulate_product',dict(code='3301002_',lead_days=-1,review_days=None,growth_pct=None,stock=None,in_transit=None,seasonality=None))


class AgentLoop(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('agenttester', password='test')

    @staticmethod
    def response(output):
        return {'output':output,'usage':{'input_tokens':100,'output_tokens':50}}

    @patch('procurement.agent.config.api_key',return_value='test-key')
    @patch('procurement.agent.call_api')
    def test_real_tools_run_and_usage_is_settled(self, api, key):
        api.side_effect=[self.response([{'type':'function_call','call_id':'c1','name':'get_product','arguments':'{"code":"3301004_"}'}]),
                         self.response([{'type':'message','content':[{'type':'output_text','text':'Поставка позже исчерпания остатка.'}]}])]
        result=agent.answer(self.user,'techno','Почему срочно?',[])
        self.assertEqual(len(result['trace']),1)
        self.assertEqual(result['trace'][0]['result']['product']['recommended_quantity'],0)
        self.assertEqual(result['usage']['cost_usd'],.00025)
        self.assertEqual(AgentBudget.objects.get(pk=1).accounted_micro,250)
        self.assertEqual(OrderLine.objects.count(),0)
        self.assertEqual(CalculationRun.objects.count(),0)
        self.assertFalse(api.call_args.args[0]['store'])

    @patch('procurement.agent.config.api_key',return_value='test-key')
    @patch('procurement.agent.call_api',side_effect=TimeoutError())
    def test_uncertain_timeout_keeps_reservation(self, api, key):
        with self.assertRaises(agent.AgentError):
            agent.answer(self.user,'techno','Риски',[])
        self.assertEqual(AgentBudget.objects.get(pk=1).accounted_micro,agent_config.RESERVATION_MICRO)
        self.assertIsNone(AgentCall.objects.get().actual_micro)

    @patch('procurement.agent.config.api_key',return_value='test-key')
    @patch('procurement.agent.call_api')
    def test_exhausted_budget_prevents_api_call(self, api, key):
        AgentBudget.objects.create(pk=1,accounted_micro=agent_config.BUDGET_MICRO)
        with self.assertRaises(agent.AgentError):
            agent.answer(self.user,'techno','Риски',[])
        api.assert_not_called()

    @patch('procurement.agent.config.api_key',return_value='')
    @patch('procurement.agent.call_api')
    def test_missing_key_is_not_presented_as_ai(self, api, key):
        with self.assertRaises(agent.AgentError):
            agent.answer(self.user,'techno','Риски',[])
        api.assert_not_called()
        self.assertEqual(AgentCall.objects.count(),0)

    @patch('procurement.agent.config.api_key',return_value='test-key')
    @patch('procurement.agent.call_api')
    def test_invalid_tool_returned_as_error_not_executed(self, api, key):
        api.side_effect=[self.response([{'type':'function_call','call_id':'c1','name':'send_supplier_order','arguments':'{}'}]),
                         self.response([{'type':'message','content':[{'type':'output_text','text':'Отправка недоступна.'}]}])]
        result=agent.answer(self.user,'techno','Отправь заказ',[])
        self.assertIn('error',result['trace'][0]['result'])


class AgentEndpoints(TestCase):
    def setUp(self):
        cache.clear()
        self.user=get_user_model().objects.create_user('agentweb',password='test')
        self.client.force_login(self.user)

    def test_viewer_cannot_spend_api_budget(self):
        response=self.client.post('/agent/ask/',data=json.dumps({'supplier':'techno','message':'Привет'}),content_type='application/json')
        self.assertEqual(response.status_code,403)

    def test_bad_input_is_rejected(self):
        self.user.user_permissions.add(Permission.objects.get(codename='edit_order'))
        response=self.client.post('/agent/ask/',data=json.dumps({'supplier':'unknown','message':'Привет'}),content_type='application/json')
        self.assertEqual(response.status_code,400)

    @patch('procurement.agent.answer')
    def test_supplier_switch_clears_context(self, answer):
        self.user.user_permissions.add(Permission.objects.get(codename='edit_order'))
        session=self.client.session
        session['agent_supplier']='iek'
        session['agent_history']=[{'role':'user','content':'old supplier question'}]
        session.save()
        answer.return_value={'answer':'Готово','trace':[],'sources':[]}
        response=self.client.post('/agent/ask/',data=json.dumps({'supplier':'techno','message':'Риски'}),content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(answer.call_args.args[3],[])

    def test_agent_page_renders_without_exposing_key(self):
        with patch('procurement.agent_views.agent_config.api_key',return_value='secret-test-key'):
            response=self.client.get('/agent/')
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'secret-test-key')
        self.assertContains(response,'ТехноСезон')
