"""
株価データ取得・テクニカル指標計算
yfinance を使って日本株の実際の株価を取得します
"""
import yfinance as yf
import pandas as pd
from typing import Dict, List, Optional

# 監視銘柄リスト（東証上場・セクター分散）
WATCHLIST = {
    "7203.T": "トヨタ自動車",
    "6758.T": "ソニーグループ",
    "9984.T": "ソフトバンクG",
    "8306.T": "三菱UFJ銀行",
    "9432.T": "NTT",
    "4063.T": "信越化学工業",
    "6861.T": "キーエンス",
    "7267.T": "本田技研工業",
    "8058.T": "三菱商事",
    "2914.T": "JT",
}


def get_current_price(ticker: str) -> Optional[float]:
    """指定銘柄の現在株価を取得"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"⚠️  {ticker} の現在価格取得エラー: {e}")
        return None


def get_prices_bulk(tickers: List[str]) -> Dict[str, Optional[float]]:
    """複数銘柄の現在株価を一括取得（高速）"""
    result = {}
    try:
        data = yf.download(tickers, period="2d", progress=False, auto_adjust=True)
        if data.empty:
            return {t: None for t in tickers}

        close = data["Close"] if len(tickers) > 1 else data[["Close"]]
        for ticker in tickers:
            try:
                col = ticker if len(tickers) > 1 else "Close"
                prices = close[col].dropna()
                result[ticker] = float(prices.iloc[-1]) if not prices.empty else None
            except Exception:
                result[ticker] = None
    except Exception as e:
        print(f"⚠️  一括価格取得エラー: {e}")
        result = {t: None for t in tickers}
    return result


def get_history(ticker: str, days: int = 60) -> Optional[pd.DataFrame]:
    """
    指定銘柄の株価履歴を取得する
    返り値: Date, Open, High, Low, Close, Volume のDataFrame
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{days}d", auto_adjust=True)
        if hist.empty:
            return None
        hist.index = hist.index.tz_localize(None)  # タイムゾーンを除去
        return hist
    except Exception as e:
        print(f"⚠️  {ticker} の履歴取得エラー: {e}")
        return None


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    テクニカル指標を計算して追加する
    - MA5: 5日移動平均
    - MA25: 25日移動平均
    - RSI14: 14日RSI（買われすぎ/売られすぎの指標）
    """
    df = df.copy()
    close = df["Close"]

    # 移動平均
    df["MA5"] = close.rolling(window=5).mean()
    df["MA25"] = close.rolling(window=25).mean()

    # RSI (Relative Strength Index)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI14"] = 100 - (100 / (1 + rs))

    return df.round(2)


def get_stock_summary(ticker: str) -> Optional[Dict]:
    """
    AI取引判断に必要な銘柄情報をまとめて取得する
    直近30日の価格・テクニカル指標を含む
    """
    df = get_history(ticker, days=60)
    if df is None or len(df) < 25:
        return None

    df = calc_indicators(df)
    recent = df.tail(30)

    # 直近のデータを取得
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    return {
        "ticker": ticker,
        "company_name": WATCHLIST.get(ticker, ticker),
        "current_price": round(float(latest["Close"]), 1),
        "prev_close": round(float(prev["Close"]), 1),
        "change_pct": round((float(latest["Close"]) / float(prev["Close"]) - 1) * 100, 2),
        "ma5": round(float(latest["MA5"]), 1) if not pd.isna(latest["MA5"]) else None,
        "ma25": round(float(latest["MA25"]), 1) if not pd.isna(latest["MA25"]) else None,
        "rsi14": round(float(latest["RSI14"]), 1) if not pd.isna(latest["RSI14"]) else None,
        "volume": int(latest["Volume"]),
        # 直近30日の終値（チャート用）
        "price_history": [
            {
                "date": str(idx.date()),
                "open": round(float(row["Open"]), 1),
                "high": round(float(row["High"]), 1),
                "low": round(float(row["Low"]), 1),
                "close": round(float(row["Close"]), 1),
                "volume": int(row["Volume"]),
                "ma5": round(float(row["MA5"]), 1) if not pd.isna(row["MA5"]) else None,
                "ma25": round(float(row["MA25"]), 1) if not pd.isna(row["MA25"]) else None,
            }
            for idx, row in recent.iterrows()
        ],
    }


def get_all_watchlist_summaries() -> List[Dict]:
    """監視銘柄全銘柄のサマリーを取得"""
    summaries = []
    for ticker in WATCHLIST:
        summary = get_stock_summary(ticker)
        if summary:
            summaries.append(summary)
        else:
            print(f"⚠️  {ticker} のデータ取得をスキップしました")
    return summaries
