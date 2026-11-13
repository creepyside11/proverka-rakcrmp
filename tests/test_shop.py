import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from storage import Store, PostgresConnection
import shop
import payments

class ShopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=Store(':memory:'); self.store.init_shop()
        self.product={'id':'p1','category':'scripts','name':'Скрипт','description':'Описание',
            'price_kopecks':19900,'file_id':'file1','filename':'script.lua','instruction':'Поместите файл в папку scripts'}
        self.store.product_add(self.product)
        self.bot=NS(send_document=AsyncMock(),send_message=AsyncMock())
    def tearDown(self): self.store.db.close()
    def add_order(self):
        self.store.order_add({**self.product,'id':'order1','buyer':10,'product_id':'p1', 'provider':'sbp',
            'provider_id':'tx1','payment_url':'https://example.com','state':'pending','created_at':time.time()})

    async def test_never_deliver_unpaid_or_other_buyer(self):
        self.add_order()
        with patch('payments.paid',new_callable=AsyncMock,return_value=False) as check:
            self.assertEqual(await shop.check_order(self.bot,self.store,'order1',11),'not_found')
            check.assert_not_awaited()
            self.assertEqual(await shop.check_order(self.bot,self.store,'order1',10),'pending')
        self.bot.send_document.assert_not_awaited()
        self.assertEqual(self.store.order('order1')['state'],'pending')

    async def test_paid_order_snapshot_and_idempotency(self):
        self.add_order(); self.store.product_set_active('p1',False)
        with patch('payments.paid',new_callable=AsyncMock,return_value=True):
            self.assertEqual(await shop.check_order(self.bot,self.store,'order1',10),'delivered')
            self.assertEqual(await shop.check_order(self.bot,self.store,'order1',10),'delivered')
        self.bot.send_document.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.order('order1')['state'],'delivered')

    async def test_other_user_cannot_edit_catalog(self):
        q=NS(data='shop:adminproduct:p1',message=NS(chat=NS(type='private')),answer=AsyncMock())
        await shop.callback(NS(callback_query=q,effective_user=NS(id=5)),NS(),self.store,1)
        self.assertTrue(self.store.product('p1')['active'])
        self.assertTrue(q.answer.await_args.kwargs['show_alert'])

    def test_postgres_query_adaptation(self):
        q=PostgresConnection.convert('INSERT OR IGNORE INTO words VALUES(?,?,?)')
        self.assertEqual(q,'INSERT INTO words VALUES(%s,%s,%s) ON CONFLICT DO NOTHING')
        self.assertIn('"user"',PostgresConnection.convert('SELECT * FROM captchas WHERE user=?'))
        self.assertEqual(shop.home_markup().to_dict()['inline_keyboard'][0][0]['style'],'success')

class PaymentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order={'id':'o1','provider':'sbp','provider_id':'tx1','price_kopecks':19900}
    async def test_payment_requires_matching_id_amount_currency_and_payload(self):
        response={'id':'tx1','status':'CONFIRMED','paymentDetails':{'amount':199,'currency':'RUB'},'payload':'o1'}
        with patch('payments._settings',return_value=('https://example.test',{})), patch('payments._request',new_callable=AsyncMock,return_value=response):
            self.assertTrue(await payments.paid(self.order))
            response['payload']='other'; self.assertFalse(await payments.paid(self.order))
            response['payload']='o1'; response['paymentDetails']['amount']=1; self.assertFalse(await payments.paid(self.order))
            response['paymentDetails']['amount']=199; response['status']='PENDING'; self.assertFalse(await payments.paid(self.order))

    async def test_crypto_invoice_requires_matching_fiat_amount_and_payload(self):
        order={'id':'o2','provider':'crypto','provider_id':'42','price_kopecks':5000}
        item={'invoice_id':42,'status':'paid','payload':'o2','currency_type':'fiat','fiat':'RUB','amount':'50.00'}
        with patch('payments._settings',return_value=('https://example.test',{})), patch('payments._request',new_callable=AsyncMock,return_value={'ok':True,'result':{'items':[item]}}):
            self.assertTrue(await payments.paid(order))
            item['status']='active'; self.assertFalse(await payments.paid(order))
            item['status']='paid'; item['fiat']='USD'; self.assertFalse(await payments.paid(order))
            item['fiat']='RUB'; item['payload']='different'; self.assertFalse(await payments.paid(order))
