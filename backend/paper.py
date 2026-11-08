from .database import connect
from .config import settings
from .risk import risk_manager

def account():
    with connect() as db: rows=db.execute('SELECT * FROM trades').fetchall()
    realized=sum(float(r['pnl'] or 0) for r in rows if r['status']=='CLOSED')
    opened=[dict(r) for r in rows if r['status']=='OPEN']
    return {'starting_balance':settings.paper_starting_balance,'realized_pnl':realized,'unrealized_pnl':0.0,'equity':settings.paper_starting_balance+realized,'open_trades':opened}

def open_trade(exchange,symbol,market_type,side,entry,stop,target=None,signal_id=None):
    if side not in ('LONG','SHORT'): raise ValueError('Side must be LONG or SHORT')
    if (side=='LONG' and stop>=entry) or (side=='SHORT' and stop<=entry): raise ValueError('Stop must be below entry for LONG and above entry for SHORT')
    a=account(); d=risk_manager.size(a['equity'],entry,stop)
    if not d.approved: raise ValueError(d.reason)
    with connect() as db:
        cur=db.execute('INSERT INTO trades(exchange,symbol,market_type,side,quantity,entry,stop,target,signal_id) VALUES(?,?,?,?,?,?,?,?,?)',(exchange,symbol,market_type,side,d.quantity,entry,stop,target,signal_id))
        tid=cur.lastrowid
    return {'id':tid,'quantity':d.quantity,'notional':d.notional,'risk_amount':d.risk_amount,'status':'OPEN'}

def close_trade(trade_id,exit_price):
    with connect() as db:
        row=db.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'",(trade_id,)).fetchone()
        if not row: raise ValueError('Open paper trade not found')
        direction=1 if row['side']=='LONG' else -1
        pnl=(exit_price-row['entry'])*row['quantity']*direction
        db.execute("UPDATE trades SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,exit_price=?,pnl=? WHERE id=?",(exit_price,pnl,trade_id))
    return {'id':trade_id,'exit_price':exit_price,'pnl':pnl,'status':'CLOSED'}

def journal():
    with connect() as db:
        trades=[dict(r) for r in db.execute('SELECT * FROM trades ORDER BY id DESC').fetchall()]
        signals=[dict(r) for r in db.execute('SELECT * FROM signals ORDER BY id DESC LIMIT 300').fetchall()]
    return {'trades':trades,'signals':signals}
