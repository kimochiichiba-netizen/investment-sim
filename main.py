"""
メインサーバー（清原式ファンダメンタル投資シミュレーター）
FastAPI + APScheduler を統合したサーバーです
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    init_db,
    get_account,
    get_portfolio,
    get_trades,
    get_asset_history,
    get_screened_stocks,
    reset_all,
)
from fundamental_data import get_fundamental, get_current_price
from fundamental_trader import (
    run_fundamental_trading,
    run_sell_check,
    run_buy_execution,
    calc_total_assets,
    _get_prices_for_portfolio,
)
from screener import run_screening

# タイムゾーン
JST = ZoneInfo("Asia/Tokyo")

# FastAPIアプリ
app = FastAPI(title="清原式ファンダメンタル投資シミュレーター", version="2.0.0")

# 静的ファイル（index.html）
app.mount("/static", StaticFiles(directory="static"), name="static")

# データベース初期化
init_db()

# ==================== スケジューラー設定 ====================

scheduler = BackgroundScheduler(timezone=JST)


def scheduled_sell_check():
    """【毎分実行】保有銘柄の売りシグナルチェック（2倍達成・損切り）"""
    now = datetime.now(JST)
    if now.weekday() >= 5:
        return  # 土日はスキップ
    if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)):
        return  # 東証営業時間外はスキップ
    run_sell_check()


def scheduled_screening_and_buy():
    """【毎朝7時実行】スクリーニング実行 → 買い付け"""
    now = datetime.now(JST)
    if now.weekday() >= 5:
        return  # 土日はスキップ
    print(f"\n⏰ 朝のスクリーニング・買い付けを開始します ({now.strftime('%Y-%m-%d %H:%M')})")
    run_screening(verbose=False)
    run_buy_execution()


# 1分ごと: 売りシグナルチェック
scheduler.add_job(
    scheduled_sell_check,
    IntervalTrigger(minutes=1, timezone=JST),
    id="sell_check",
    name="売りシグナルチェック（1分ごと）",
    replace_existing=True,
)

# 毎朝7:00: スクリーニング + 買い付け
scheduler.add_job(
    scheduled_screening_and_buy,
    CronTrigger(hour=7, minute=0, timezone=JST),
    id="morning_screening",
    name="朝のスクリーニング・買い付け（毎朝7時）",
    replace_existing=True,
)

scheduler.start()
print("✅ AI自動取引スケジューラーを開始しました（平日 東証営業時間中・1分ごと）")


# ==================== APIエンドポイント ====================

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/status")
async def get_status():
    """総資産・現金・損益サマリーを返す"""
    account   = get_account()
    portfolio = get_portfolio()
    prices    = _get_prices_for_portfolio(portfolio) if portfolio else {}

    total_assets = calc_total_assets(account["cash"], portfolio, prices)
    stock_value  = total_assets - account["cash"]
    pnl          = total_assets - account["initial_capital"]
    pnl_pct      = (pnl / account["initial_capital"]) * 100 if account["initial_capital"] else 0

    return {
        "total_assets":    round(total_assets, 0),
        "cash":            round(account["cash"], 0),
        "stock_value":     round(stock_value, 0),
        "initial_capital": account["initial_capital"],
        "pnl":             round(pnl, 0),
        "pnl_pct":         round(pnl_pct, 2),
        "updated_at":      datetime.now(JST).isoformat(),
    }


@app.get("/api/portfolio")
async def get_portfolio_api():
    """保有株一覧（現在価格・含み損益・ファンダメンタル指標付き）を返す"""
    portfolio = get_portfolio()
    if not portfolio:
        return {"holdings": [], "total_stock_value": 0}

    prices = _get_prices_for_portfolio(portfolio)

    holdings = []
    total_stock_value = 0
    for h in portfolio:
        current_price = prices.get(h["ticker"]) or h["avg_cost"]
        market_value  = current_price * h["shares"]
        cost_basis    = h["avg_cost"] * h["shares"]
        unrealized_pnl     = market_value - cost_basis
        unrealized_pnl_pct = (unrealized_pnl / cost_basis) * 100 if cost_basis else 0
        total_stock_value += market_value

        holdings.append({
            "ticker":             h["ticker"],
            "company_name":       h["company_name"],
            "shares":             h["shares"],
            "avg_cost":           round(h["avg_cost"], 1),
            "current_price":      round(current_price, 1),
            "market_value":       round(market_value, 0),
            "unrealized_pnl":     round(unrealized_pnl, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            # ファンダメンタル指標（買い付け時に記録したもの）
            "buy_per":            h.get("buy_per"),
            "buy_pbr":            h.get("buy_pbr"),
            "buy_net_cash_ratio": h.get("buy_net_cash_ratio"),
            "target_price":       h.get("target_price"),
            "catalyst_notes":     h.get("catalyst_notes"),
        })

    return {
        "holdings":          holdings,
        "total_stock_value": round(total_stock_value, 0),
    }


@app.get("/api/trades")
async def get_trades_api(limit: int = 50):
    trades = get_trades(limit)
    return {"trades": trades}


@app.get("/api/asset-history")
async def get_asset_history_api(days: int = 30):
    history = get_asset_history(days)
    return {"history": history}


@app.get("/api/scheduler/status")
async def get_scheduler_status():
    """スケジューラーの状態を返す"""
    sell_job     = scheduler.get_job("sell_check")
    morning_job  = scheduler.get_job("morning_screening")
    return {
        "running": scheduler.running,
        "sell_check_next":    sell_job.next_run_time.astimezone(JST).isoformat() if sell_job and sell_job.next_run_time else None,
        "morning_run_next":   morning_job.next_run_time.astimezone(JST).isoformat() if morning_job and morning_job.next_run_time else None,
    }


# ── 清原式専用エンドポイント ──────────────────────────────

@app.get("/api/screened")
async def get_screened_api():
    """スクリーニング通過銘柄の一覧を返す（スコア降順）"""
    stocks = get_screened_stocks()
    return {"screened": stocks, "count": len(stocks)}


@app.post("/api/screening/run")
async def run_screening_api():
    """スクリーニングを手動で実行する（時間がかかります）"""
    print("🔴 手動スクリーニングを開始")
    results = run_screening(verbose=True)
    return {
        "status":  "success",
        "count":   len(results),
        "message": f"{len(results)}銘柄がスクリーニングを通過しました",
        "top5": [
            {
                "ticker":          r["ticker"],
                "company_name":    r["company_name"],
                "net_cash_ratio":  r.get("net_cash_ratio"),
                "score":           r.get("score"),
            }
            for r in results[:5]
        ],
    }


@app.get("/api/fundamental/{ticker}")
async def get_fundamental_api(ticker: str):
    """指定銘柄の財務データ詳細を返す"""
    # セキュリティ: ティッカーの形式チェック
    if not ticker.endswith(".T") or len(ticker) > 12:
        raise HTTPException(status_code=400, detail="無効なティッカー形式です")

    data = get_fundamental(ticker)
    if not data:
        raise HTTPException(status_code=404, detail="財務データを取得できませんでした")

    price = get_current_price(ticker)
    if price:
        data["current_price"] = price

    return data


@app.post("/api/trade/run")
async def run_trade_now():
    """清原式自動売買を手動で即時実行する（売りチェック + 買い付け）"""
    print("🔴 手動トリガーによる清原式自動売買を開始")
    result = run_fundamental_trading()
    return result


@app.post("/api/reset")
async def reset_data():
    """全データをリセットして初期状態（200万円）に戻す"""
    reset_all()
    return {"status": "ok", "message": "データをリセットしました。初期資金200万円からスタートします。"}


# ==================== 起動 ====================

if __name__ == "__main__":
    import uvicorn
    print("🚀 清原式ファンダメンタル投資シミュレーターを起動します")
    print("📌 ブラウザで http://localhost:8000 を開いてください")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
