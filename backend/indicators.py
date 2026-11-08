def ema(values, period):
    if len(values) < period: raise ValueError('Not enough values for EMA')
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]: value = item * alpha + value * (1-alpha)
    return value

def rsi(values, period=14):
    if len(values) <= period: raise ValueError('Not enough values for RSI')
    changes = [b-a for a,b in zip(values[:-1], values[1:])]
    gains = [max(x,0) for x in changes]; losses = [max(-x,0) for x in changes]
    avg_gain = sum(gains[:period])/period; avg_loss = sum(losses[:period])/period
    for g,l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain*(period-1)+g)/period; avg_loss = (avg_loss*(period-1)+l)/period
    return 100.0 if avg_loss == 0 else 100 - 100/(1+avg_gain/avg_loss)

def atr(candles, period=14):
    if len(candles) <= period: raise ValueError('Not enough candles for ATR')
    tr=[]
    for prev,cur in zip(candles[:-1],candles[1:]):
        tr.append(max(cur[2]-cur[3],abs(cur[2]-prev[4]),abs(cur[3]-prev[4])))
    return sum(tr[-period:])/period

def analyze(candles):
    closes=[float(c[4]) for c in candles]
    if len(closes)<50: raise ValueError('At least 50 candles required')
    e20,e50=ema(closes,20),ema(closes,50); strength=rsi(closes); volatility=atr(candles); last=closes[-1]
    trend='bullish' if last>e20>e50 else 'bearish' if last<e20<e50 else 'sideways'
    score=(1 if e20>e50 else -1 if e20<e50 else 0)+(1 if strength>=55 else -1 if strength<=45 else 0)+(1 if last>e20 else -1 if last<e20 else 0)
    signal='LONG' if score>=2 else 'SHORT' if score<=-2 else 'WAIT'
    return {'price':last,'ema20':e20,'ema50':e50,'rsi14':strength,'atr14':volatility,'trend':trend,'score':score,'signal':signal,'stop_distance':max(volatility*2,last*.005)}
