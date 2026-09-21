"""User-operated ADA Spot integration. Import/status/tests never send live orders.

Live requires the local launcher, credentials, and explicit --live flag.
No withdrawal, leverage, short, or use of unallocated quote balance.
"""
import argparse
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
import uuid

from strategy_research import features, signals

ROOT = Path(__file__).resolve().parent
DB = ROOT/'state/ada-live.sqlite3'
SYMBOL = 'ADAUSDT'
CAP = Decimal('294')
CONTRACT = 'ada-4h-ema20-50-200-stop2atr-target4atr-ioc-v1'
CONFIRM = '294 ADA ILE GERCEK ISLEM BASLAT'
INTERVAL = '4h'
STEP_SECONDS = 14400
MODEL_DECISIONS = False
USE_ALL_ALLOCATED_FUNDS = False
AUTO_ALLOCATE_SPOT = False


class ApiError(RuntimeError):
    def __init__(self, message, status, code):
        super().__init__(message)
        self.status = status
        self.code = code


def configure_interval(interval):
    global INTERVAL, STEP_SECONDS, CONTRACT
    if interval not in ('4h', '15m'):
        raise ValueError('Unsupported ADA interval')
    INTERVAL = interval
    STEP_SECONDS = {'4h':14400, '15m':900}[interval]
    CONTRACT = f'ada-{interval}-ema20-50-200-stop2atr-target4atr-ioc-v1'


def migrate_interval(db, client):
    """User-run, under the worker lock; preserve funds and existing exit levels."""
    s = read(db)
    if not s or s['contract'] == CONTRACT:
        return
    if INTERVAL != '15m' or s['contract'] != 'ada-4h-ema20-50-200-stop2atr-target4atr-ioc-v1':
        raise ValueError('Unsupported interval migration')
    if s['identity'] != client.identity or s['pending'] or s['halted'] or client.open_orders():
        raise ValueError('Migration requires matching key, healthy ledger and no pending/open orders')
    free = balances(client.account())
    if any(free.get(asset, Decimal(0)) < dec(s[field]) for asset, field in [('ADA','ada'),('USDT','usdt')]):
        raise ValueError('Allocated capital unavailable during migration')
    fee_rates(client.commission())
    previous = json.dumps(s)
    s.update(contract=CONTRACT, interval=INTERVAL, last_bar=None,
             learning={'status':'awaiting_15m_data','decision_authority':False})
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS interval_changes(id INTEGER PRIMARY KEY,ts REAL,previous_state TEXT,new_contract TEXT)')
        db.execute('INSERT INTO interval_changes(ts,previous_state,new_contract) VALUES (?,?,?)',
                   (time.time(),previous,CONTRACT))
        write(db,s)


def ada_learner():
    # Private module instance: never change the Testnet/paper learner globals.
    import importlib.util
    spec = importlib.util.spec_from_file_location('ada_interval_learner', ROOT/'trend4h_learning.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FRAME = INTERVAL
    module.STEP = STEP_SECONDS * 1000
    module.VERSION = f'ridge-next{INTERVAL}-v1'
    return module


def dec(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError('Invalid non-negative financial value')
    return number


def grid(value, step, up=False):
    return (value/step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*step


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('API redirect refused')


class Client:
    def __init__(self, *, live=False):
        if not live or os.getenv('ADA_LIVE_AUTHORIZATION') != CONFIRM:
            raise ValueError('Local explicit live authorization required')
        self.key = os.environ.get('ADA_MAINNET_API_KEY','')
        self.secret = os.environ.get('ADA_MAINNET_SECRET_KEY','')
        if not self.key or not self.secret:
            raise ValueError('Missing local mainnet credentials')
        self.identity = hashlib.sha256(self.key.encode()).hexdigest()
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, params=None, signed=False):
        reads = {'time','exchangeInfo','ticker/bookTicker','klines','account','account/commission','openOrders','order','myTrades','allOrders'}
        if not (method=='GET' and path in reads or method=='POST' and path in {'order','order/test'}):
            raise ValueError('Unsupported API operation')
        values = dict(params or {})
        if signed:
            clock = self.request('GET','time')['serverTime']
            values.update(timestamp=int(clock),recvWindow=5000)
        query = urlencode(values)
        headers = {}
        if signed:
            query += '&signature='+hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
            headers['X-MBX-APIKEY'] = self.key
        url = 'https://api.binance.com/api/v3/'+path
        request = Request(url+('?' + query if method=='GET' and query else ''),
                          data=query.encode() if method=='POST' else None,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            # Only a numeric exchange code and local fixed descriptions are safe.
            code = None
            try:
                body = json.loads(exc.read(8192))
                candidate = body.get('code') if isinstance(body,dict) else None
                if type(candidate) is int and -99999 <= candidate < 0:
                    code = candidate
            except Exception:
                pass
            finally:
                exc.close()
            hints = {-2015:'Check API key, allowed IP and Spot trading permissions',
                     -2014:'Invalid API key format', -1022:'Invalid request signature',
                     -1021:'Request timestamp outside allowed window',
                     -2013:'Order not found; pending record retained for review',
                     -1002:'Request not authorized'}
            detail = f'; Binance code {code}' if code is not None else ''
            if code in hints:
                detail += '; '+hints[code]
            raise ApiError(f'API HTTP {exc.code}{detail}; no automatic order retry',exc.code,code) from None
        except Exception:
            raise RuntimeError('API transport/response failure; order outcome may be unknown') from None

    def account(self):
        return self.request('GET','account',signed=True)

    def commission(self):
        return self.request('GET','account/commission',{'symbol':SYMBOL},True)

    def open_orders(self):
        return self.request('GET','openOrders',{'symbol':SYMBOL},True)

    def market(self):
        info = self.request('GET','exchangeInfo',{'symbol':SYMBOL})
        symbol = next(s for s in info['symbols'] if s['symbol']==SYMBOL)
        if symbol['status']!='TRADING' or not symbol.get('isSpotTradingAllowed',False) or 'LIMIT' not in symbol['orderTypes']:
            raise ValueError('ADA Spot limit trading unavailable')
        book = self.request('GET','ticker/bookTicker',{'symbol':SYMBOL})
        bid,ask = dec(book['bidPrice']),dec(book['askPrice'])
        if not 0 < bid <= ask:
            raise ValueError('Invalid book')
        raw = self.request('GET','klines',{'symbol':SYMBOL,'interval':INTERVAL,'limit':1000})
        now = int(self.request('GET','time')['serverTime'])
        rows = [dict(ts=int(r[0]),open=float(r[1]),high=float(r[2]),low=float(r[3]),
                     close=float(r[4]),volume=float(r[5])) for r in raw if int(r[6]) < now]
        from agent import validate
        rows = validate(rows,STEP_SECONDS)
        if len(rows)<205 or not 0 <= now-rows[-1]['ts']-STEP_SECONDS*1000 <= STEP_SECONDS*1000:
            raise ValueError(f'Insufficient or stale {INTERVAL} history')
        f = features(rows)
        enter,leave = signals(rows,f,'trend_hysteresis','balanced')
        return dict(bid=str(bid),ask=str(ask),filters=symbol['filters'],
                    bar=rows[-1]['ts'],now=now/1000,atr=str(f['atr'][-1]),
                    enter=bool(enter[-1]),leave=bool(leave[-1]),learning_rows=rows)

    def submit(self, intent):
        return self.request('POST','order',dict(symbol=SYMBOL,side=intent['side'],type='LIMIT',
            timeInForce='IOC',quantity=intent['qty'],price=intent['price'],
            newClientOrderId=intent['client_id'],newOrderRespType='FULL'),True)

    def test_order(self, intent):
        # Binance validates TRADE permissions without sending to matching engine.
        return self.request('POST','order/test',dict(symbol=SYMBOL,side=intent['side'],type='LIMIT',
            timeInForce='IOC',quantity=intent['qty'],price=intent['price']),True)

    def lookup(self, intent):
        order = self.request('GET','order',{'symbol':SYMBOL,'origClientOrderId':intent['client_id']},True)
        if dec(order['executedQty']) > 0:
            trades = self.request('GET','myTrades',{'symbol':SYMBOL,'orderId':order['orderId'],'limit':1000},True)
            order['fills'] = [dict(qty=t['qty'],price=t['price'],commission=t['commission'],
                                  commissionAsset=t['commissionAsset']) for t in trades if t['orderId']==order['orderId']]
        return order


def connect(path=DB):
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=10)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,client_id TEXT UNIQUE,request TEXT,response TEXT,applied INTEGER DEFAULT 0)')
    db.commit()
    return db


def read(db):
    row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def write(db,s):
    db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(json.dumps(s,allow_nan=False),))


def balances(account):
    if account.get('canTrade') is not True:
        raise ValueError('Account cannot trade')
    items = account['balances']
    if len({r['asset'] for r in items}) != len(items):
        raise ValueError('Duplicate account balances')
    return {r['asset']:dec(r['free']) for r in items}


def fee_rates(data):
    if data.get('symbol')!=SYMBOL:
        raise ValueError('Wrong commission market')
    discount = data.get('discount',{})
    if discount.get('enabledForAccount') and discount.get('enabledForSymbol'):
        raise ValueError('Disable BNB fee payment locally first; external fee assets are not allocated')
    rates = {}
    for side,field in [('BUY','buyer'),('SELL','seller')]:
        total = Decimal(0)
        for name in ('standardCommission','specialCommission','taxCommission'):
            row = data[name]
            total += dec(row['taker'])+dec(row[field])
        if total >= Decimal('.01'):
            raise ValueError('Commission is outside supported sizing range')
        rates[side] = total
    return rates


def allocation_snapshot(client, path=None):
    path = DB if path is None else path
    account = client.account()
    free = balances(account)
    items = {r['asset']:r for r in account['balances']}
    s = None
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            s = read(db)
        finally:
            db.close()
    if s and s['identity']!=client.identity:
        raise ValueError('Account binding mismatch')
    rows = {}
    for asset,field in [('ADA','ada'),('USDT','usdt')]:
        available = free.get(asset,Decimal(0))
        tracked = dec(s[field]) if s else Decimal(0)
        rows[asset] = dict(free=str(available),locked=str(dec(items.get(asset,{}).get('locked','0'))),
                           tracked=str(tracked),shortfall=str(max(Decimal(0),tracked-available)))
    return dict(assets=rows,allocation_available=all(dec(r['shortfall'])==0 for r in rows.values()),
                ledger_present=bool(s))


def diagnose(client, check_allocation=True):
    """Read-only checks. Never calls submit or mutates the trading ledger."""
    report = {'orders_submitted':0,'checks':{}}
    checks = [('account',lambda: allocation_snapshot(client) if check_allocation else balances(client.account())),
              ('commission',lambda: fee_rates(client.commission())),
              ('market',client.market),
              ('open_orders',lambda: not bool(client.open_orders()))]
    for name,check in checks:
        try:
            value = check()
            report['checks'][name] = {'ok': value is not False}
            if name=='account' and check_allocation:
                report['allocation'] = value
                report['checks']['allocated_balance'] = {'ok':value['allocation_available']}
        except (ValueError,RuntimeError) as exc:
            report['checks'][name] = {'ok':False,'reason':str(exc)}
        except Exception as exc:
            report['checks'][name] = {'ok':False,'reason':type(exc).__name__}
    return report


def check_trade_permission(client, path=DB):
    """No real order and no ledger writes, including when an intent is pending."""
    report = {'orders_submitted':0,'test_endpoint':'/api/v3/order/test','ledger_modified':False}
    try:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            s = read(db)
        finally:
            db.close()
        if not s or s['identity']!=client.identity or s['contract']!=CONTRACT:
            raise ValueError('Account/strategy binding mismatch')
        market = client.market()
        rates = fee_rates(client.commission())
        free = balances(client.account())
        if free.get('ADA',Decimal(0)) < dec(s['ada']) or free.get('USDT',Decimal(0)) < dec(s['usdt']):
            raise ValueError('Allocated balance changed; reconcile first')
        if client.open_orders():
            raise ValueError('Open ADA orders require reconciliation first')
        side = 'SELL' if s['phase']=='long' else 'BUY'
        intent = size(s,market,side,rates[side])
        if intent is None:
            raise ValueError('No valid test quantity within allocated funds')
        response = client.test_order(intent)
        if response != {}:
            raise ValueError('Unexpected test-order response')
        report.update(ok=True,side=side)
    except (ValueError,RuntimeError) as exc:
        report.update(ok=False,reason=str(exc))
    except Exception as exc:
        report.update(ok=False,reason=type(exc).__name__)
    return report


def recover_unsent(db, client):
    """User-run recovery of the first rejected sell only; never submits an order."""
    s = read(db)
    if not s or s['identity']!=client.identity or s['contract']!=CONTRACT:
        raise ValueError('Account/strategy binding mismatch')
    if (not s['pending'] or not s['halted'] or s['filled_orders'] or
            dec(s['ada'])!=CAP or dec(s['usdt'])!=0 or s['phase']!='long' or
            not s.get('halt_reason','').startswith('API HTTP 401;')):
        raise ValueError('Recovery is limited to the initial unfilled sell rejected with HTTP 401')
    records = db.execute('SELECT request,response,applied FROM orders').fetchall()
    if len(records)!=1 or records[0][1] is not None or records[0][2]:
        raise ValueError('Order history requires manual reconciliation')
    intent = json.loads(records[0][0])
    if intent['client_id']!=s['pending'] or intent['side']!='SELL':
        raise ValueError('Unexpected pending intent')
    now = int(client.request('GET','time')['serverTime'])
    start = int(dec(intent['created'])*1000)-60000
    if not 120000 <= now-start < 86400000:
        raise ValueError('Recovery requires an intent aged 1 minute to less than 24 hours')
    def require_absent():
        try:
            client.lookup(intent)
        except ApiError as exc:
            if exc.status==400 and exc.code==-2013:
                return
            raise
        raise ValueError('Order exists; use ReconcileOnly')
    require_absent()
    if client.open_orders():
        raise ValueError('Open ADA orders prevent recovery')
    for endpoint in ('allOrders','myTrades'):
        history = client.request('GET',endpoint,dict(symbol=SYMBOL,startTime=start,endTime=now,limit=1000),True)
        if not isinstance(history,list) or history:
            raise ValueError('Nonempty or invalid ADA history requires manual reconciliation')
    free = balances(client.account())
    if free.get('ADA',Decimal(0)) < CAP:
        raise ValueError('Initial ADA allocation is unavailable')
    require_absent()
    proof = dict(kind='verified_absent_initial_401',checked_ms=now,
                 history_start_ms=start,order_queries_absent=2,
                 all_orders_count=0,trades_count=0,orders_submitted=0)
    previous = json.dumps(s)
    s.update(pending=None,halted=None,exit_requested=False,stopped=True,
             last_recovery=proof)
    s.pop('halt_reason',None)
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS recoveries(id INTEGER PRIMARY KEY,client_id TEXT,previous_state TEXT,evidence TEXT)')
        db.execute('INSERT INTO recoveries(client_id,previous_state,evidence) VALUES (?,?,?)',
                   (intent['client_id'],previous,json.dumps(proof)))
        db.execute('UPDATE orders SET applied=1,response=? WHERE client_id=?',
                   (json.dumps(proof),intent['client_id']))
        write(db,s)
    return dict(ok=True,orders_submitted=0,recovered_client_id=intent['client_id'],worker_started=False)


def adopt_spot_balance(db, client):
    """Explicit user-run capital rebase after an external conversion; GETs only."""
    s = read(db)
    if not s or s['identity']!=client.identity or s['contract']!=CONTRACT:
        raise ValueError('Account/strategy binding mismatch')
    if not USE_ALL_ALLOCATED_FUNDS:
        raise ValueError('Balance adoption requires --use-all-allocated-funds')
    if s['pending'] or db.execute('SELECT COUNT(*) FROM orders WHERE applied=0').fetchone()[0]:
        raise ValueError('Unresolved order prevents capital rebase')
    if s.get('halt_reason')!='Allocated capital unavailable; external account change':
        raise ValueError('Capital rebase is only for the external balance-change halt')
    if client.open_orders():
        raise ValueError('Open ADA orders prevent capital rebase')
    market = client.market()
    fee_rates(client.commission())
    account = client.account()
    free = balances(account)
    for row in account['balances']:
        if row['asset'] in ('ADA','USDT') and dec(row.get('locked','0')):
            raise ValueError('Locked ADA/USDT prevents capital rebase')
    ada,usdt = free.get('ADA',Decimal(0)),free.get('USDT',Decimal(0))
    bid,atr = dec(market['bid']),dec(market['atr'])
    if bid<=0 or atr<=0 or (ada>0 and bid<=2*atr) or ada*bid+usdt<=0:
        raise ValueError('Invalid balance or market for capital rebase')
    # Catch balance changes during the checks; never silently import locked funds.
    second = client.account()
    second_free = balances(second)
    if any(second_free.get(asset,Decimal(0))!=value for asset,value in [('ADA',ada),('USDT',usdt)]):
        raise ValueError('Balance changed during verification; retry diagnosis')
    if any(row['asset'] in ('ADA','USDT') and dec(row.get('locked','0')) for row in second['balances']):
        raise ValueError('Funds became locked during verification')
    if client.open_orders():
        raise ValueError('Order appeared during verification')
    previous = json.dumps(s)
    baseline = ada*bid+usdt
    s.update(ada=str(ada),usdt=str(usdt),initial_mark_usdt=str(baseline),
             pnl_basis='external_rebase_mark_not_trade_profit',
             marked_equity_usdt=str(baseline),marked_pnl_from_activation_usdt='0',
             external_capital_inflows_usdt='0',
             phase='long' if ada>0 else 'cash',stop=str(max(Decimal(0),bid-2*atr)),
             target=str(bid+4*atr),opened=market['now'],last_bar=None,last_poll=None,
             halted=None,stopped=True,exit_requested=False,use_all_allocated_funds=True)
    s.pop('halt_reason',None)
    s.pop('decision',None)
    s.pop('latest_candle_decision',None)
    s.setdefault('learning',{})['decision_authority']=False
    record = dict(kind='user_authorized_external_spot_rebase',ada=str(ada),usdt=str(usdt),
                  reference_bid=str(bid),baseline_usdt=str(baseline),
                  checked_at=market['now'],not_agent_profit=True)
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS capital_rebases(id INTEGER PRIMARY KEY,previous_state TEXT,evidence TEXT)')
        cur=db.execute('INSERT INTO capital_rebases(previous_state,evidence) VALUES (?,?)',(previous,json.dumps(record)))
        s['capital_epoch']=cur.lastrowid
        s['last_capital_rebase']=record
        write(db,s)
    return dict(ok=True,orders_submitted=0,worker_started=False,ada=str(ada),usdt=str(usdt),
                new_epoch_baseline_usdt=str(baseline),use_all_allocated_funds=True)


def initialize(db, client, market):
    s = read(db)
    if s:
        if s['identity']!=client.identity or s['contract']!=CONTRACT:
            raise ValueError('Account/strategy does not match this ledger')
        return s
    free = balances(client.account())
    if free.get('ADA',Decimal(0)) < CAP or client.open_orders():
        raise ValueError('Need 294 free ADA and no existing ADAUSDT orders')
    reference,atr = dec(market['bid']),dec(market['atr'])
    if atr <= 0 or reference <= atr*2:
        raise ValueError('Invalid initial ATR')
    s = dict(contract=CONTRACT,interval=INTERVAL,identity=client.identity,ada=str(CAP),usdt='0',
             initial_mark_usdt=str(CAP*reference),pnl_basis='initial_mark_not_original_purchase_cost',
             phase='long',stop=str(reference-2*atr),target=str(reference+4*atr),
             opened=market['now'],last_bar=None,pending=None,halted=None,
             exit_requested=False,filled_orders=0,last_poll=None,stopped=False)
    with db:
        write(db,s)
    return s


def prepare_new_key(db,client):
    """User-requested rebind is supported only before any order intent exists."""
    s=read(db)
    if s and (s['contract']!=CONTRACT or s['pending'] or s['filled_orders'] or
              dec(s['ada'])!=CAP or dec(s['usdt'])!=0 or s['last_bar'] is not None):
        raise ValueError('New-key shortcut requires an untouched 294 ADA allocation')
    if db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:
        raise ValueError('Existing order history requires separate account reconciliation')
    report=diagnose(client,check_allocation=False)
    (ROOT/'reports/ada-live-diagnosis.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    if not all(check['ok'] for check in report['checks'].values()):
        reasons=[name+': '+check.get('reason','check failed') for name,check in report['checks'].items() if not check['ok']]
        raise ValueError('; '.join(reasons))
    if balances(client.account()).get('ADA',Decimal(0)) < CAP:
        raise ValueError('New key must expose at least 294 free ADA')
    if s:
        previous=dict(s)
        s.update(identity=client.identity,halted=None)
        s.pop('halt_reason',None)
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS key_changes(id INTEGER PRIMARY KEY,ts REAL,previous_state TEXT,new_fingerprint TEXT)')
            db.execute('INSERT INTO key_changes(ts,previous_state,new_fingerprint) VALUES (?,?,?)',
                       (time.time(),json.dumps(previous),client.identity))
            write(db,s)


def size(s,market,side,rate):
    filters = {x['filterType']:x for x in market['filters']}
    lot, pf = filters['LOT_SIZE'],filters['PRICE_FILTER']
    step,tick = dec(lot['stepSize']),dec(pf['tickSize'])
    if not step or not tick:
        raise ValueError('Missing quantity/price steps')
    # IOC caps limit price and never reserves more than tracked capital + fees.
    price = grid(dec(market['ask'])*Decimal('1.001') if side=='BUY' else dec(market['bid'])*Decimal('.999'),tick,side=='BUY')
    if side=='BUY':
        available = dec(s['usdt'])/(price*(1+rate))
        if not USE_ALL_ALLOCATED_FUNDS:
            available = min(CAP-dec(s['ada']),available)
    else:
        available = dec(s['ada'])/(1+rate)
    qty = grid(min(available,dec(lot['maxQty'])),step)
    if price <= 0 or qty <= 0 or qty < dec(lot['minQty']):
        return None
    for name in ('minPrice','maxPrice'):
        bound = dec(pf[name])
        if bound and ((name=='minPrice' and price < bound) or (name=='maxPrice' and price > bound)):
            raise ValueError('Price outside exchange bounds')
    notional = qty*price
    for kind in ('MIN_NOTIONAL','NOTIONAL'):
        rule = filters.get(kind)
        if rule and notional < dec(rule['minNotional']):
            return None
        if kind=='NOTIONAL' and rule and notional > dec(rule['maxNotional']):
            qty = grid(dec(rule['maxNotional'])/price,step)
            if qty < dec(lot['minQty']) or qty*price < dec(rule['minNotional']):
                return None
    return dict(side=side,qty=str(qty),price=str(price))


def settle(db,client):
    s = read(db)
    if not s or s['identity']!=client.identity or s['contract']!=CONTRACT:
        raise ValueError('Account/strategy binding mismatch')
    if not s['pending']:
        return s
    row = db.execute('SELECT request,applied FROM orders WHERE client_id=?',(s['pending'],)).fetchone()
    if not row or row[1]:
        raise ValueError('Inconsistent pending order')
    intent = json.loads(row[0])
    order = client.lookup(intent)
    if order.get('clientOrderId')!=intent['client_id'] or order.get('symbol')!=SYMBOL or order.get('side')!=intent['side']:
        raise ValueError('Order identity mismatch')
    if order['status'] not in ('FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED'):
        raise ValueError('Order remains pending; no further submissions')
    qty,quote = dec(order['executedQty']),dec(order['cummulativeQuoteQty'])
    if qty > dec(intent['qty']) or (order['status']=='FILLED' and qty != dec(intent['qty'])):
        raise ValueError('Overfilled order')
    fills = order.get('fills',[])
    if sum((dec(f['qty']) for f in fills),Decimal(0)) != qty:
        raise ValueError('Incomplete fill evidence')
    if abs(sum((dec(f['qty'])*dec(f['price']) for f in fills),Decimal(0))-quote) > Decimal('.00000001'):
        raise ValueError('Quote/fill mismatch')
    fees = {'ADA':Decimal(0),'USDT':Decimal(0)}
    for fill in fills:
        if fill['commissionAsset'] not in fees and dec(fill['commission']) != 0:
            raise ValueError('Unallocated commission asset; reconciliation required')
        if fill['commissionAsset'] in fees:
            fees[fill['commissionAsset']] += dec(fill['commission'])
    ada,usdt = dec(s['ada']),dec(s['usdt'])
    if intent['side']=='BUY':
        if quote > qty*dec(intent['price'])+Decimal('.00000001'):
            raise ValueError('Buy execution exceeds limit')
        ada += qty-fees['ADA']; usdt -= quote+fees['USDT']
        if qty:
            s.update(phase='long',opened=intent['created'],stop=intent['stop'],target=intent['target'])
    else:
        if quote+Decimal('.00000001') < qty*dec(intent['price']):
            raise ValueError('Sell execution below limit')
        ada -= qty+fees['ADA']; usdt += quote-fees['USDT']
    if ada < 0 or usdt < 0 or (not USE_ALL_ALLOCATED_FUNDS and ada > CAP):
        raise ValueError('Fill violates allocated capital')
    s.update(ada=str(ada),usdt=str(usdt),pending=None,filled_orders=s['filled_orders']+int(qty>0))
    with db:
        db.execute('UPDATE orders SET applied=1,response=? WHERE client_id=?',(json.dumps(order),intent['client_id']))
        write(db,s)
    return s


def tick(db,client):
    existing = read(db)
    if existing and existing.get('use_all_allocated_funds') and not USE_ALL_ALLOCATED_FUNDS:
        raise ValueError('Restart with --use-all-allocated-funds to preserve allocation mode')
    if existing and existing['identity']!=client.identity:
        raise ValueError('Account binding mismatch')
    if existing and existing['pending']:
        return settle(db,client)  # Query only; never resubmit an uncertain intent.
    if existing and existing['halted']:
        return existing
    market = client.market()
    observed_at = time.monotonic()
    decision_clock = lambda: market['now'] + time.monotonic() - observed_at
    s = initialize(db,client,market)
    free = balances(client.account())
    if free.get('ADA',Decimal(0)) < dec(s['ada']) or free.get('USDT',Decimal(0)) < dec(s['usdt']):
        raise ValueError('Allocated capital unavailable; external account change')
    if client.open_orders():
        raise ValueError('Untracked ADAUSDT orders exist')
    rates = fee_rates(client.commission())
    if AUTO_ALLOCATE_SPOT:
        s = allocate_spot_increases(db,client,s,market)
    if 'learning_rows' in market:
        try:
            learner = ada_learner()
            learning_path = ROOT/('state/ada-live-learning.sqlite3' if INTERVAL=='4h' else 'state/ada-live-15m-learning.sqlite3')
            learned = learner.update(market['learning_rows'],market['now'],path=learning_path,clock=decision_clock)
            s['learning'] = {k:learned[k] for k in ('status','model_id','label_counts','forward_scored_predictions')}
            s['learning'].update(symbol=SYMBOL,interval=INTERVAL,decision_authority=False)
        except Exception as exc:
            s['learning'] = dict(status='error',error=type(exc).__name__,decision_authority=False)
    fresh = s['last_bar'] is None or market['bar'] > s['last_bar']
    s['model_decisions_enabled'] = MODEL_DECISIONS
    s['decision'] = dict(owner='ema_atr',model_decisions_requested=MODEL_DECISIONS)
    if MODEL_DECISIONS and INTERVAL == '15m' and fresh:
        from ada_model_decisions import decision
        if s.get('learning',{}).get('status') == 'error':
            s['decision']['reason'] = 'learning_refresh_failed'
        else:
            s['decision'] = decision(ROOT/'state/ada-live-15m-learning.sqlite3',market['bar'],decision_clock())
        if s['decision']['owner'] == 'learned_model':
            market = dict(market,enter=s['decision']['target_long'],leave=not s['decision']['target_long'])
    s.setdefault('learning',{})['decision_authority'] = s['decision']['owner'] == 'learned_model'
    if fresh:
        s['latest_candle_decision'] = dict(s['decision'],bar=market['bar'])
    bid,atr = dec(market['bid']),dec(market['atr'])
    if atr <= 0:
        raise ValueError('Invalid ATR')
    side = None
    if s['phase']=='long':
        should_exit = (s['exit_requested'] or bid <= dec(s['stop']) or bid >= dec(s['target'])
                       or market['now']-s['opened'] >= 192*3600 or fresh and market['leave'])
        if should_exit:
            side='SELL'; s['exit_requested']=True
    elif fresh and market['enter'] and 0 <= market['now']-market['bar']/1000-STEP_SECONDS <= 300:
        side='BUY'
    s.update(last_bar=market['bar'],last_poll=market['now'],stopped=False,
             use_all_allocated_funds=USE_ALL_ALLOCATED_FUNDS,
             auto_allocate_spot=AUTO_ALLOCATE_SPOT,
             marked_equity_usdt=str(dec(s['usdt'])+dec(s['ada'])*bid))
    s['marked_pnl_from_activation_usdt'] = str(dec(s['marked_equity_usdt'])-dec(s['initial_mark_usdt'])
                                             -dec(s.get('external_capital_inflows_usdt','0')))
    intent = size(s,market,side,rates[side]) if side else None
    if side=='SELL' and intent is None:
        # Residual untradeable ADA remains owned/accounted; no external top-up.
        s.update(phase='cash',exit_requested=False)
    if not intent:
        with db:
            write(db,s)
        return s
    if side=='BUY' and dec(intent['price']) <= 2*atr:
        raise ValueError('Invalid entry stop')
    intent.update(client_id='qra-'+uuid.uuid4().hex[:28],created=market['now'],
                  stop=str(dec(intent['price'])-2*atr),target=str(dec(intent['price'])+4*atr))
    s['pending']=intent['client_id']
    with db:
        db.execute('INSERT INTO orders(client_id,request) VALUES (?,?)',(intent['client_id'],json.dumps(intent)))
        write(db,s)  # Durable before POST. A crash never causes an automatic repeat.
    client.submit(intent)
    return settle(db,client)


def allocate_spot_increases(db,client,s,market):
    """Under worker lock: account increases are capital flows, never trading PnL."""
    if not USE_ALL_ALLOCATED_FUNDS or not AUTO_ALLOCATE_SPOT:
        raise ValueError('Automatic allocation requires both explicit allocation flags')
    if s['identity']!=client.identity or s['contract']!=CONTRACT or s['halted'] or s['pending']:
        raise ValueError('Unresolved ledger prevents automatic allocation')
    if db.execute('SELECT COUNT(*) FROM orders WHERE applied=0').fetchone()[0]:
        raise ValueError('Unapplied order prevents automatic allocation')
    def snapshot():
        account=client.account()
        free=balances(account)
        if any(r['asset'] in ('ADA','USDT') and dec(r.get('locked','0')) for r in account['balances']):
            raise ValueError('Locked ADA/USDT requires reconciliation')
        return {asset:free.get(asset,Decimal(0)) for asset in ('ADA','USDT')}
    free=snapshot()
    deltas={asset:free[asset]-dec(s[field]) for asset,field in [('ADA','ada'),('USDT','usdt')]}
    if any(v<0 for v in deltas.values()):
        raise ValueError('Allocated capital unavailable; external account change')
    if not any(deltas.values()):
        return s
    if client.open_orders() or snapshot()!=free or client.open_orders():
        raise ValueError('Account changed during allocation; reconciliation required')
    bid,atr=dec(market['bid']),dec(market['atr'])
    if bid<=0 or atr<=0:
        raise ValueError('Invalid allocation market')
    added=deltas['USDT']+deltas['ADA']*bid
    result=dict(s)
    result.update(ada=str(free['ADA']),usdt=str(free['USDT']),
                  external_capital_inflows_usdt=str(dec(s.get('external_capital_inflows_usdt','0'))+added),
                  use_all_allocated_funds=True,auto_allocate_spot=True)
    if deltas['ADA']>0 and s['phase']=='cash' and size(result,market,'SELL',Decimal(0)) is not None:
        if bid<=2*atr:
            raise ValueError('Invalid stop for incoming ADA')
        result.update(phase='long',opened=market['now'],stop=str(bid-2*atr),target=str(bid+4*atr))
    result['marked_equity_usdt']=str(free['USDT']+free['ADA']*bid)
    result['marked_pnl_from_activation_usdt']=str(dec(result['marked_equity_usdt'])
        -dec(result['initial_mark_usdt'])-dec(result['external_capital_inflows_usdt']))
    evidence=dict(kind='external_balance_increase_not_trade_profit',ada_delta=str(deltas['ADA']),
                  usdt_delta=str(deltas['USDT']),value_usdt=str(added),reference_bid=str(bid),
                  observed_at=market['now'],capital_epoch=s.get('capital_epoch',0))
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS capital_flows(id INTEGER PRIMARY KEY,previous_state TEXT,evidence TEXT)')
        cursor=db.execute('INSERT INTO capital_flows(previous_state,evidence) VALUES (?,?)',(json.dumps(s),json.dumps(evidence)))
        result['last_capital_flow']=dict(evidence,id=cursor.lastrowid)
        write(db,result)
    return result


@contextmanager
def singleton(path):
    import msvcrt
    with path.open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        try:
            yield
        finally:
            handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)


def main():
    global MODEL_DECISIONS, USE_ALL_ALLOCATED_FUNDS, AUTO_ALLOCATE_SPOT
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','run','reconcile','diagnose','order-check','recover-unsent','adopt-spot-balance'])
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--new-key',action='store_true')
    parser.add_argument('--interval',choices=['4h','15m'],default='4h')
    parser.add_argument('--migrate-interval',action='store_true')
    parser.add_argument('--model-decisions',action='store_true')
    parser.add_argument('--use-all-allocated-funds',action='store_true')
    parser.add_argument('--auto-allocate-spot',action='store_true')
    args=parser.parse_args()
    configure_interval(args.interval)
    MODEL_DECISIONS = args.model_decisions
    USE_ALL_ALLOCATED_FUNDS = args.use_all_allocated_funds
    AUTO_ALLOCATE_SPOT = args.auto_allocate_spot
    if AUTO_ALLOCATE_SPOT and (not USE_ALL_ALLOCATED_FUNDS or args.action!='run'):
        raise ValueError('Auto allocation requires run --use-all-allocated-funds')
    if MODEL_DECISIONS and (args.interval!='15m' or args.action!='run'):
        raise ValueError('Experimental model decisions require run --interval 15m')
    if args.migrate_interval and (args.action!='run' or args.new_key):
        raise ValueError('Interval migration requires run without new-key')
    if args.action=='status':
        if not DB.exists():
            print(json.dumps({'status':'not_started','real_orders_submitted':0})); return
        db=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)
        s=read(db); db.close()
        if s:
            s.pop('identity',None)
        print(json.dumps(s,indent=2)); return
    client=Client(live=args.live)
    if args.action=='order-check':
        report = check_trade_permission(client)
        print(json.dumps(report,indent=2))
        return 0 if report['ok'] else 1
    if args.action=='diagnose':
        report=diagnose(client)
        (ROOT/'reports/ada-live-diagnosis.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
        return
    DB.parent.mkdir(parents=True,exist_ok=True)
    with singleton(DB.with_suffix('.lock')):
        db=connect()
        try:
            if args.action=='adopt-spot-balance':
                print(json.dumps(adopt_spot_balance(db,client),indent=2)); return
            if args.action=='recover-unsent':
                print(json.dumps(recover_unsent(db,client),indent=2)); return
            if args.migrate_interval:
                migrate_interval(db,client)
            if args.new_key:
                if args.action!='run':
                    raise ValueError('New-key preparation requires run action')
                prepare_new_key(db,client)
            if args.action=='reconcile':
                settle(db,client)
                print('Reconciled only; trading remains halted until reviewed.'); return
            while True:
                try:
                    s=tick(db,client)
                    if s['halted']:
                        print('HALTED: inspect ledger; no automatic restart.'); return 1
                except Exception as exc:
                    s=read(db)
                    if s:
                        s['halted']=type(exc).__name__
                        s['halt_reason'] = str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
                        with db:
                            write(db,s)
                    print('HALTED: '+type(exc).__name__+'; inspect status/pending order.'); return 1
                time.sleep(60)
        except KeyboardInterrupt:
            print('Stopped locally. Holdings remain in account; no shutdown sell.')
        finally:
            db.close()


if __name__=='__main__':
    raise SystemExit(main())
