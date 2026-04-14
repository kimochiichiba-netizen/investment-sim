"""
自動売買エンジン（ルールベース版）
Claude APIを使わず、テクニカル指標のみで売買判断を行います

【使用するルール】
■ 買いサイン（両方満たすとき）
  - ゴールデンクロス: 5日移動平均 > 25日移動平均
  - RSI（14日）が 60 以下（過熱していない）

■ 売りサイン（どちらか満たすとき）
  - デッドクロス: 5日移動平均 < 25日移動平均
  - RSI（14日）が 70 以上（買われすぎ）
  - 含み損が -10% を超えた（損切り）

■ リスク管理
  - 1回の取引は総資産の5%以下
  - 1銘柄への投資上限は総資産の20%
  - 現金は常に総資産の30%以上を維持
  - 手数料: 取引額の0.1%（最低100円）
"""
from datetime import datetime
from typing import List, Dict, Optional

from database import (
    get_account,
    get_portfolio,
    get_holding,
    upsert_holding,
    delete_holding,
    save_trade,
    update_cash,
    save_asset_snapshot,
)
from stock_data import get_all_watchlist_summaries, WATCHLIST

# リスク管理パラメータ
MAX_SINGLE_TRADE_RATIO = 0.05   # 1回の取引は総資産の5%まで
MAX_SINGLE_STOCK_RATIO = 0.20   # 1銘柄は総資産の20%まで
MIN_CASH_RATIO         = 0.30   # 現金は総資産の30%以上を維持
COMMISSION_RATE        = 0.001  # 手数料 0.1%
MIN_COMMISSION         = 100    # 最低手数料 100円

# テクニカル指標のしきい値
RSI_BUY_THRESHOLD  = 60   # これ以下なら買い検討
RSI_SELL_THRESHOLD = 70   # これ以上なら売り検討
STOP_LOSS_PCT      = -10  # 含み損がこの%を超えたら損切り


def calc_commission(amount: float) -> float:
    """手数料を計算する（0.1%、最低100円）"""
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)


def calc_total_assets(cash: float, portfolio: List[Dict], prices: Dict[str, float]) -> float:
    """総資産を計算する（現金 + 保有株の時価評価額）"""
    stock_value = sum(
        h["shares"] * prices.get(h["ticker"], h["avg_cost"])
        for h in portfolio
    )
    return cash + stock_value


def _judge_action(summary: Dict, holding: Optional[Dict]) -> tuple[str, int, str]:
    """
    テクニカル指標から売買判断を行う

    返り値: (action, shares, reason)
      action: "buy" / "sell" / "hold"
      shares: 売買する株数
      reason: 判断理由
    """
    ticker  = summary["ticker"]
    price   = summary["current_price"]
    ma5     = summary.get("ma5")
    ma25    = summary.get("ma25")
    rsi     = summary.get("rsi14")

    # 指標が揃っていない場合はスキップ
    if ma5 is None or ma25 is None or rsi is None:
        return "hold", 0, "指標データが不足しているため様子見"

    # ゴールデンクロス（上昇トレンドに入った）
    golden_cross = ma5 > ma25
    # デッドクロス（下降トレンドに入った）
    dead_cross   = ma5 < ma25

    # 損切りチェック（保有中のみ）
    if holding and holding["shares"] > 0:
        pnl_pct = (price / holding["avg_cost"] - 1) * 100
        if pnl_pct <= STOP_LOSS_PCT:
            return "sell", holding["shares"], f"損切り（含み損 {pnl_pct:.1f}%、MA5={ma5:,}円）"

    # 売りサイン
    if rsi >= RSI_SELL_THRESHOLD:
        if holding and holding["shares"] > 0:
            # 半分売る（一括売りはリスクが高いため）
            sell_shares = max(1, holding["shares"] // 2)
            return "sell", sell_shares, f"RSI過熱（{rsi:.1f}）、利確のため{sell_shares}株売却"
        return "hold", 0, f"RSI過熱（{rsi:.1f}）だが未保有のため見送り"

    if dead_cross and holding and holding["shares"] > 0:
        sell_shares = max(1, holding["shares"] // 2)
        return "sell", sell_shares, f"デッドクロス（MA5={ma5:,}円 < MA25={ma25:,}円）、{sell_shares}株売却"

    # 買いサイン
    if golden_cross and rsi <= RSI_BUY_THRESHOLD:
        return "buy", 0, f"ゴールデンクロス（MA5={ma5:,}円 > MA25={ma25:,}円）かつRSI={rsi:.1f}で割安"

    # 様子見
    trend = "上昇" if golden_cross else "下降"
    return "hold", 0, f"{trend}トレンド・RSI={rsi:.1f}、現在は様子見"


def _calc_buy_shares(price: float, total_assets: float, ticker: str) -> int:
    """
    買える株数を計算する（リスク管理ルールに基づく）
    """
    max_amount = total_assets * MAX_SINGLE_TRADE_RATIO  # 総資産の5%まで
    shares = int(max_amount / price)
    return max(1, shares)


def _execute_buy(ticker: str, company_name: str, shares: int, price: float,
                 reason: str, cash: float, total_assets: float) -> bool:
    """買い注文を実行する"""
    trade_amount = price * shares
    commission   = calc_commission(trade_amount)
    total_cost   = trade_amount + commission

    # 現金不足チェック
    if cash - total_cost < total_assets * MIN_CASH_RATIO:
        # 現金上限を守れる範囲で株数を減らして再挑戦
        available = cash - (total_assets * MIN_CASH_RATIO) - MIN_COMMISSION
        if available < price:
            print(f"⚠️  {ticker} 現金不足のためスキップ（残高{cash:,.0f}円）")
            return False
        shares = max(1, int(available / (price * (1 + COMMISSION_RATE))))
        trade_amount = price * shares
        commission   = calc_commission(trade_amount)
        total_cost   = trade_amount + commission

    # 1銘柄上限チェック
    holding = get_holding(ticker)
    current_val = (holding["shares"] * price) if holding else 0
    if current_val + trade_amount > total_assets * MAX_SINGLE_STOCK_RATIO:
        print(f"⚠️  {ticker} 1銘柄の上限超えのためスキップ")
        return False

    # 購入実行
    new_shares = shares + (holding["shares"] if holding else 0)
    if holding:
        new_avg_cost = (holding["avg_cost"] * holding["shares"] + trade_amount) / new_shares
    else:
        new_avg_cost = price

    upsert_holding(ticker, company_name, new_shares, new_avg_cost)
    update_cash(cash - total_cost)
    save_trade(ticker, company_name, "buy", shares, price,
               trade_amount, commission, reason)
    print(f"✅ 買い: {company_name}({ticker}) {shares}株 @{price:,.0f}円 手数料{commission:.0f}円")
    return True


def _execute_sell(ticker: str, company_name: str, shares: int, price: float,
                  reason: str, cash: float) -> bool:
    """売り注文を実行する"""
    holding = get_holding(ticker)
    if not holding or holding["shares"] < shares:
        print(f"⚠️  {ticker} 保有株不足のためスキップ")
        return False

    trade_amount = price * shares
    commission   = calc_commission(trade_amount)
    proceeds     = trade_amount - commission

    new_shares = holding["shares"] - shares
    if new_shares <= 0:
        delete_holding(ticker)
    else:
        upsert_holding(ticker, company_name, new_shares, holding["avg_cost"])

    update_cash(cash + proceeds)
    save_trade(ticker, company_name, "sell", shares, price,
               trade_amount, commission, reason)
    print(f"✅ 売り: {company_name}({ticker}) {shares}株 @{price:,.0f}円 手数料{commission:.0f}円")
    return True


def run_ai_trading() -> Dict:
    """
    自動取引を実行するメイン関数

    1. 全銘柄の市場データ（株価・テクニカル指標）を取得
    2. ルールベースで売買判断
    3. 判断を実行
    4. 結果を返す
    """
    print(f"\n{'='*50}")
    print(f"📊 自動取引開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    # 市場データ取得
    print("📡 市場データを取得中...")
    stock_summaries = get_all_watchlist_summaries()
    if not stock_summaries:
        return {"status": "error", "message": "市場データの取得に失敗しました"}

    # 現在の資産状況
    account   = get_account()
    portfolio = get_portfolio()
    prices    = {s["ticker"]: s["current_price"] for s in stock_summaries}
    total_assets = calc_total_assets(account["cash"], portfolio, prices)

    print(f"💰 総資産: {total_assets:,.0f}円 / 現金: {account['cash']:,.0f}円")

    executed = []
    skipped  = []

    for summary in stock_summaries:
        ticker       = summary["ticker"]
        company_name = summary["company_name"]
        price        = summary["current_price"]
        holding      = get_holding(ticker)

        # 最新の現金残高を取得（前の取引で変わっているため）
        current_cash = get_account()["cash"]

        action, shares, reason = _judge_action(summary, holding)
        print(f"  {company_name}: {action.upper()} - {reason}")

        if action == "buy":
            buy_shares = _calc_buy_shares(price, total_assets, ticker)
            success = _execute_buy(
                ticker, company_name, buy_shares, price,
                reason, current_cash, total_assets
            )
            (executed if success else skipped).append(f"buy:{ticker}")

        elif action == "sell" and shares > 0:
            success = _execute_sell(
                ticker, company_name, shares, price, reason, current_cash
            )
            (executed if success else skipped).append(f"sell:{ticker}")

    # 取引後の資産を記録（グラフ用）
    account_after  = get_account()
    portfolio_after = get_portfolio()
    total_after    = calc_total_assets(account_after["cash"], portfolio_after, prices)
    stock_value    = total_after - account_after["cash"]
    save_asset_snapshot(total_after, account_after["cash"], stock_value)

    result = {
        "status": "success",
        "executed_at": datetime.now().isoformat(),
        "total_assets_before": round(total_assets, 0),
        "total_assets_after":  round(total_after, 0),
        "pnl": round(total_after - account["initial_capital"], 0),
        "executed_trades": executed,
        "skipped_trades":  skipped,
    }
    print(f"🏁 取引完了: {len(executed)}件実行 / {len(skipped)}件スキップ")
    print(f"📈 総資産: {total_assets:,.0f}円 → {total_after:,.0f}円")
    return result
