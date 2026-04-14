"""
メインサーバー
FastAPI + APScheduler を統合したサーバーです
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import (
    init_db,
    get_account,
    get_portfolio,
    get_trades,
    get_asset_history,
    reset_all,
)
from stock_data import get_stock_summary, get_prices_bulk, WATCHLIST
from ai_trader import run_ai_trading, calc_total_assets

# タイムゾーン
JST = ZoneInfo("Asia/Tokyo")

# FastAPIアプリ
app = FastAPI(title="Claude投資シミュレーター", version="1.0.0")

# 静的ファイル（index.html）
app.mount("/static", StaticFiles(directory="static"), name="static")

# データベース初期化
init_db()

# ==================== スケジューラー設定 ====================

scheduler = BackgroundScheduler(timezone=JST)

def scheduled_trade():
    """スケジュール実行される取引関数"""
    now = datetime.now(JST)
    # 東証の営業時間（平日 9:30〜15:00）のみ実行
    if now.weekday() >= 5:  # 土日はスキップ
        print(f"⏭️  土日のためスキップ ({now.strftime('%Y-%m-%d %H:%M')})")
        return
    if not (9 <= now.hour < 15):
        print(f"⏭️  東証営業時間外のためスキップ ({now.strftime('%H:%M')})")
        return
    print(f"⏰ 定期取引を開始します ({now.strftime('%Y-%m-%d %H:%M')})")
    run_ai_trading()

# 毎時00分に実行
scheduler.add_job(
    scheduled_trade,
    CronTrigger(minute=0, timezone=JST),
    id="auto_trade",
    name="自動取引",
    replace_existing=True,
)
scheduler.start()
print("✅ 自動取引スケジューラーを開始しました（平日 毎時00分）")


# ==================== APIエンドポイント ====================

@app.get("/")
async def root():
    """ダッシュボードHTMLを返す"""
    return FileResponse("static/index.html")


@app.get("/api/status")
async def get_status():
    """総資産・現金・損益サマリーを返す"""
    account = get_account()
    portfolio = get_portfolio()

    # 保有株の現在価格を取得
    tickers = [h["ticker"] for h in portfolio]
    prices = get_prices_bulk(tickers) if tickers else {}

    total_assets = calc_total_assets(account["cash"], portfolio, prices)
    stock_value = total_assets - account["cash"]
    pnl = total_assets - account["initial_capital"]
    pnl_pct = (pnl / account["initial_capital"]) * 100

    return {
        "total_assets": round(total_assets, 0),
        "cash": round(account["cash"], 0),
        "stock_value": round(stock_value, 0),
        "initial_capital": account["initial_capital"],
        "pnl": round(pnl, 0),
        "pnl_pct": round(pnl_pct, 2),
        "updated_at": datetime.now(JST).isoformat(),
    }


@app.get("/api/portfolio")
async def get_portfolio_api():
    """保有株一覧（現在価格・含み損益付き）を返す"""
    portfolio = get_portfolio()
    if not portfolio:
        return {"holdings": [], "total_stock_value": 0}

    tickers = [h["ticker"] for h in portfolio]
    prices = get_prices_bulk(tickers)

    holdings = []
    total_stock_value = 0
    for h in portfolio:
        current_price = prices.get(h["ticker"]) or h["avg_cost"]
        market_value = current_price * h["shares"]
        cost_basis = h["avg_cost"] * h["shares"]
        unrealized_pnl = market_value - cost_basis
        unrealized_pnl_pct = (unrealized_pnl / cost_basis) * 100 if cost_basis else 0
        total_stock_value += market_value

        holdings.append({
            "ticker": h["ticker"],
            "company_name": h["company_name"],
            "shares": h["shares"],
            "avg_cost": round(h["avg_cost"], 1),
            "current_price": round(current_price, 1),
            "market_value": round(market_value, 0),
            "unrealized_pnl": round(unrealized_pnl, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        })

    return {
        "holdings": holdings,
        "total_stock_value": round(total_stock_value, 0),
    }


@app.get("/api/trades")
async def get_trades_api(limit: int = 50):
    """取引履歴を返す（最新順）"""
    trades = get_trades(limit)
    return {"trades": trades}


@app.get("/api/chart/{ticker}")
async def get_chart_data(ticker: str):
    """指定銘柄の30日チャートデータを返す"""
    # セキュリティ: ウォッチリストの銘柄のみ許可
    if ticker not in WATCHLIST:
        raise HTTPException(status_code=400, detail="対象外の銘柄です")

    summary = get_stock_summary(ticker)
    if not summary:
        raise HTTPException(status_code=404, detail="データ取得に失敗しました")

    return summary


@app.get("/api/watchlist")
async def get_watchlist():
    """監視銘柄リストを返す"""
    tickers = list(WATCHLIST.keys())
    prices = get_prices_bulk(tickers)
    return {
        "watchlist": [
            {
                "ticker": t,
                "company_name": WATCHLIST[t],
                "current_price": prices.get(t),
            }
            for t in tickers
        ]
    }


@app.post("/api/trade/run")
async def run_trade_now():
    """AI取引を手動で即時実行する"""
    print("🔴 手動トリガーによるAI取引を開始")
    result = run_ai_trading()
    return result


@app.get("/api/asset-history")
async def get_asset_history_api(days: int = 30):
    """資産推移履歴を返す（グラフ用）"""
    history = get_asset_history(days)
    return {"history": history}


@app.get("/api/scheduler/status")
async def get_scheduler_status():
    """スケジューラーの状態と次回実行時刻を返す"""
    job = scheduler.get_job("auto_trade")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.astimezone(JST).isoformat()
    return {
        "running": scheduler.running,
        "next_run_time": next_run,
    }


@app.post("/api/reset")
async def reset_data():
    """全データをリセットして初期状態に戻す"""
    reset_all()
    return {"status": "ok", "message": "データをリセットしました。初期資金100万円からスタートします。"}


# ==================== 起動 ====================

if __name__ == "__main__":
    import uvicorn
    print("🚀 Claude投資シミュレーターを起動します")
    print("📌 ブラウザで http://localhost:8000 を開いてください")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
