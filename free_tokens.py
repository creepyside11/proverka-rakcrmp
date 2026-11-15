"""Daily, per-model allowances usable only by the authenticated creator."""
import time
from aiohttp import web
import ai_gateway
import ai_models

QUOTAS={'deepseek_flash':5_000_000,'glm':4_000_000,'gpt_sol':2_000_000,'gpt_astra':2_000_000}
COOLDOWN=24*60*60


def init(store):
    store.db.executescript('''CREATE TABLE IF NOT EXISTS ai_free_claims (
      owner_id BIGINT NOT NULL, model_key TEXT NOT NULL, claimed_at DOUBLE PRECISION NOT NULL,
      PRIMARY KEY(owner_id,model_key));''')
    store.db.commit()


def ref(uid,model):return f'free:{uid}:{model}'


def state(store,uid,now=None):
    now=time.time() if now is None else now
    rows=store.db.execute('SELECT model_key,claimed_at FROM ai_free_claims WHERE owner_id=?',(uid,))
    claims={r['model_key']:r['claimed_at'] for r in rows}
    rows=store.db.execute("SELECT model_key,balance,in_flight FROM ai_keys WHERE buyer=? AND order_ref LIKE ?",(uid,'free:%'))
    balances={r['model_key']:r for r in rows}
    return [{'model':model,'name':ai_models.MODELS[model],'amount':amount,
             'balance':balances[model]['balance'] if model in balances else 0,
             'pending':bool(balances[model]['in_flight']) if model in balances else False,
             'configured':bool(ai_models.config(store,model)),
             'next_claim_at':claims[model]+COOLDOWN if model in claims else 0,
             'can_claim':model not in claims or now>=claims[model]+COOLDOWN}
            for model,amount in QUOTAS.items()]


def claim(store,bot_token,uid,model,now=None):
    if model not in QUOTAS:raise web.HTTPBadRequest(text='Неизвестная модель.')
    now=time.time() if now is None else now
    order_ref=ref(uid,model);token=ai_gateway.key_for(bot_token,order_ref)
    with ai_gateway.transaction(store):
        changed=store.db.execute('''INSERT INTO ai_free_claims(owner_id,model_key,claimed_at) VALUES(?,?,?)
            ON CONFLICT(owner_id,model_key) DO UPDATE SET claimed_at=excluded.claimed_at
            WHERE ai_free_claims.claimed_at<=?''',(uid,model,now,now-COOLDOWN))
        if changed.rowcount!=1:raise web.HTTPTooManyRequests(text='Токены этой модели уже получены. Повторите через 24 часа после предыдущего получения.')
        store.db.execute('''INSERT INTO ai_keys(order_ref,buyer,model_key,token_hash,balance,in_flight,created_at)
            VALUES(?,?,?,?,?,0,?) ON CONFLICT(order_ref) DO UPDATE SET balance=ai_keys.balance+excluded.balance''',
            (order_ref,uid,model,ai_gateway.digest(token),QUOTAS[model],now))
    return {'model':model,'amount':QUOTAS[model],'next_claim_at':now+COOLDOWN}


async def endpoint(request,authorize):
    uid=await authorize(request);store=request.app['store']
    if request.method=='GET':return web.json_response({'models':state(store,uid)},headers={'Cache-Control':'no-store'})
    try:data=await request.json()
    except (ValueError,TypeError):raise web.HTTPBadRequest(text='Некорректный запрос.')
    model=data.get('model') if isinstance(data,dict) else None
    if not isinstance(model,str):raise web.HTTPBadRequest(text='Выберите модель.')
    result=claim(store,request.app['token'],uid,model)
    return web.json_response(result,headers={'Cache-Control':'no-store'})
