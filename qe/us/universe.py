"""美股股票池:硬编码快照 + 分层(先做大盘,流动性最好)。

⚠️ 幸存者偏差:这里的名单是**今天的成分股**,历史上被剔除/退市的没进来。
回测时必须标注这个偏差 —— 所有美股策略的收益都因此被高估。
"""

SP100 = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","BRK-B","JPM",
    "LLY","V","UNH","XOM","MA","COST","HD","PG","WMT","NFLX",
    "AMD","CRM","ADBE","ORCL","CSCO","ACN","MCD","PEP","KO","TMO",
    "ABT","DHR","TXN","QCOM","INTC","MU","AMAT","LRCX","ADI","KLAC",
    "BA","CAT","GE","HON","LMT","UPS","RTX","DE","MMM","FDX",
    "GS","MS","BLK","AXP","BAC","WFC","C","SCHW","SPGI","CME",
    "JNJ","PFE","MRK","ABBV","AMGN","GILD","BMY","CVS","CI","ELV",
    "NKE","SBUX","TGT","LOW","BKNG","MAR","CMG","ORLY","AZO","YUM",
    "DIS","CMCSA","T","VZ","TMUS","NEE","DUK","SO","AEP","EXC",
    "LIN","APD","SHW","ECL","NEM","FCX","SLB","COP","OXY","PSX",
]

NDX_EXTRA = ["PLTR","COIN","MSTR","SNOW","DDOG","CRWD","PANW","NOW","ASML","ARM"]

ETF = ["SPY", "QQQ", "IWM", "DIA"]
