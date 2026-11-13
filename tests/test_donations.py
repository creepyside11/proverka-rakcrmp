import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import payments
import shop
from storage import Store


class DonationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=Store(':memory:'); self.store.init_shop()
        self.bot=NS(send_message=AsyncMock(),send_document=AsyncMock())
        self.context=NS(bot=self.bot,user_data={},bot_data={})
        self.message=NS(chat=NS(type='private',id=42),text=None,reply_text=AsyncMock())
        self.user=NS(id=42)

    def tearDown(self): self.store.db.close()

    def update(self, data):
        q=NS(data=data,message=self.message,answer=AsyncMock())
        return NS(callback_query=q,effective_user=self.user,effective_message=self.message)

    async def amount(self, text):
        self.message.text=text
        return await shop.donation_input(NS(effective_message=self.message), self.context)

    async def test_input_minimum_precision_and_selection(self):
        await shop.callback(self.update('shop:donate'),self.context,self.store,1)
        for value in ['0','0.99','-1','1.001','NaN','Infinity','1e1000000','abc','']:
            self.assertTrue(await self.amount(value))
            self.assertEqual(self.context.user_data['donation_draft']['step'],'amount',value)
        self.assertTrue(await self.amount('1,00'))
        draft=self.context.user_data['donation_draft']
        self.assertEqual(draft['amount'],100)
        markup=self.message.reply_text.await_args.kwargs['reply_markup'].to_dict()
        values=[x['callback_data'] for row in markup['inline_keyboard'] for x in row]
        self.assertEqual({s.split(':')[2] for s in values},{'crypto','sbp','card'})

    async def test_three_payment_methods_and_repeated_tap(self):
        for provider in ('crypto','sbp','card'):
            await shop.callback(self.update('shop:donate'),self.context,self.store,1)
            await self.amount('100.50')
            token=self.context.user_data['donation_draft']['token']
            update=self.update('shop:donatepay:'+provider+':'+token)
            with patch('shop.payments.create',new_callable=AsyncMock,return_value=('tx-'+provider,'https://pay.example/x')) as create:
                await shop.callback(update,self.context,self.store,1)
                await shop.callback(update,self.context,self.store,1)
                create.assert_awaited_once()
                self.assertEqual(create.await_args.args[0],provider)
                self.assertEqual(create.await_args.args[3],10050)
            self.assertNotIn('donation_draft',self.context.user_data)
        self.assertEqual(len(self.store.pending_donations()),3)
        self.assertEqual(self.store.pending_orders(),[])

    async def test_donation_confirmed_only_once_and_private_to_buyer(self):
        self.store.donation_add({'id':'id1','buyer':42,'price_kopecks':100,'provider':'sbp',
            'provider_id':'tx','payment_url':'https://pay.example','state':'pending','created_at':time.time()})
        with patch('shop.payments.paid',new_callable=AsyncMock,return_value=False) as paid:
            self.assertEqual(await shop.check_donation(self.bot,self.store,'id1',99),'not_found')
            paid.assert_not_awaited()
            self.assertEqual(await shop.check_donation(self.bot,self.store,'id1',42),'pending')
        self.bot.send_message.assert_not_awaited()
        with patch('shop.payments.paid',new_callable=AsyncMock,return_value=True):
            self.assertEqual(await shop.check_donation(self.bot,self.store,'id1',42),'notified')
            self.assertEqual(await shop.check_donation(self.bot,self.store,'id1',42),'notified')
        self.bot.send_message.assert_awaited_once()
        self.bot.send_document.assert_not_awaited()
        self.assertEqual(self.store.donation('id1')['state'],'notified')

    async def test_provider_failure_allows_retry_and_cancel(self):
        await shop.callback(self.update('shop:donate'),self.context,self.store,1)
        await self.amount('1')
        token=self.context.user_data['donation_draft']['token']
        with patch('shop.payments.create',new_callable=AsyncMock,side_effect=payments.PaymentError):
            await shop.callback(self.update('shop:donatepay:card:'+token),self.context,self.store,1)
        self.assertEqual(self.context.user_data['donation_draft']['token'],token)
        self.assertTrue(await self.amount('/cancel'))
        self.assertNotIn('donation_draft',self.context.user_data)

    async def test_admin_product_draft_not_overwritten(self):
        self.context.user_data['product_draft']={'step':'name'}
        update=self.update('shop:donate')
        await shop.callback(update,self.context,self.store,42)
        self.assertEqual(self.context.user_data['product_draft']['step'],'name')
        self.assertNotIn('donation_draft',self.context.user_data)
        self.assertTrue(update.callback_query.answer.await_args.kwargs['show_alert'])
