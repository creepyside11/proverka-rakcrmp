"""Payment providers: create invoices and confirm status with the provider API."""
import os
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import httpx

class PaymentError(Exception):
    pass

def _settings(provider):
    if provider == 'crypto':
        token = os.getenv('CRYPTO_PAY_TOKEN', '').strip()
        if not token: raise PaymentError('Crypto Bot не настроен')
        base = 'https://testnet-pay.crypt.bot/api' if os.getenv('CRYPTO_PAY_TESTNET') == '1' else 'https://pay.crypt.bot/api'
        return base, {'Crypto-Pay-API-Token': token}
    merchant = os.getenv('PLATEGA_MERCHANT_ID', '').strip()
    secret = os.getenv('PLATEGA_SECRET', '').strip()
    if not merchant or not secret: raise PaymentError('Platega не настроена')
    return 'https://app.platega.io', {'X-MerchantId': merchant, 'X-Secret': secret}

async def _request(method, url, headers, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict): raise ValueError('invalid response')
            return data
    except (httpx.HTTPError, ValueError) as exc:
        raise PaymentError('Сервис оплаты недоступен или вернул ошибку') from exc

async def create(provider, order_id, buyer, price_kopecks, name):
    base, headers = _settings(provider)
    amount = f'{Decimal(price_kopecks)/100:.2f}'
    if provider == 'crypto':
        data = await _request('POST', base+'/createInvoice', headers, json={'currency_type':'fiat', 'fiat':'RUB', 'amount':amount,
            'payload':order_id, 'description':name[:128], 'allow_comments':False, 'expires_in':3600})
        invoice = data.get('result') if data.get('ok') else None
        if not isinstance(invoice, dict): raise PaymentError('Не удалось создать счёт Crypto Bot')
        pid, url = str(invoice.get('invoice_id') or ''), invoice.get('bot_invoice_url') or invoice.get('pay_url')
    elif provider in ('sbp', 'card'):
        data = await _request('POST', base+'/transaction/process', headers, json={'paymentMethod':2 if provider=='sbp' else 11,
            'paymentDetails':{'amount':float(amount), 'currency':'RUB'}, 'description':name[:120], 'payload':order_id,
            'metadata':{'userId':str(buyer), 'user_email':f'{buyer}@telegram.org'}})
        pid, url = str(data.get('transactionId') or ''), data.get('redirect')
    else: raise PaymentError('Неизвестный способ оплаты')
    if not pid or not isinstance(url,str) or not url.startswith('https://'):
        raise PaymentError('Платёжный сервис вернул неполный ответ')
    return pid, url

async def paid(order):
    base, headers = _settings(order['provider'])
    if order['provider'] == 'crypto':
        data = await _request('GET', base+'/getInvoices', headers, params={'invoice_ids':order['provider_id']})
        items = data.get('result',{}).get('items',[]) if data.get('ok') else []
        item = items[0] if len(items)==1 else {}
        if item.get('status') != 'paid': return False
        try:
            return (str(item.get('invoice_id')) == order['provider_id'] and item.get('payload') == order['id']
                    and item.get('currency_type') == 'fiat' and item.get('fiat') == 'RUB'
                    and Decimal(str(item.get('amount'))) * 100 == order['price_kopecks'])
        except InvalidOperation: return False
    data = await _request('GET', base+'/transaction/'+quote(order['provider_id'],safe=''), headers)
    if data.get('status') != 'CONFIRMED': return False
    details = data.get('paymentDetails') or {}
    try:
        return (str(data.get('id')) == order['provider_id'] and data.get('payload') == order['id']
                and details.get('currency') == 'RUB' and Decimal(str(details.get('amount'))) * 100 == order['price_kopecks'])
    except InvalidOperation: return False
