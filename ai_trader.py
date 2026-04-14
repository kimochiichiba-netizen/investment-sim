"""
自動売買エンジン（スコアリング方式・API不要版）
複数のテクニカル指標を点数化して合算し、売買判断を行います。
APIキーは一切不要です。

【スコアリングルール】
■ 買いスコア（高いほど買いたい）
  - MA5 > MA25（上昇トレンド）: 最大+30点（差が大きいほど高得点）
  - RSI14 < 30（売られすぎ）: +35点
  - RSI14 30〜45（低め）: +20点
  - 当日値上がり +1.5%超: +15点
  - 現金比率35%以上（余裕あり）: +10点

■ 売りスコア（高いほど売りたい）
  - MA5 < MA25（下降トレンド）: 最大+30点
  - RSI14 > 70（買われすぎ）: +35点
  - RSI14 60〜70（やや高め）: +15点
  - 当日値下がり -1.5%超: +15点

■ 強制売り（スコア関係なし）
  - 含み損が -10% 以下 → 損切り（全株売り）

■ 判断しきい値
  - 買いスコア >= 55 かつ 買い > 売り → buy
  - 売りスコア >= 45 かつ 売り > 買い かつ 保有中 → sell
  - それ以外 → hold

【リスク管理（プログラムで強制するルール）】
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


def _judge_all_stocks(account: Dict, portfolio: List[Dict],
                      stock_summaries: List[Dict], prices: Dict[str, float]) -> List[Dict]:
    """
    スコアリング方式で全銘柄の売買判断をする（API不要）

    返り値:
        [{"ticker": "7203.T", "action": "buy"/"sell"/"hold", "reason": "理由"}]
    """
    total_assets = calc_total_assets(account["cash"], portfolio, prices)
    cash_ratio = account["cash"] / total_assets if total_assets > 0 else 0

    decisions = []

    for summary in stock_summaries:
        ticker      = summary["ticker"]
        price       = summary["current_price"]
        ma5         = summary.get("ma5")
        ma25        = summary.get("ma25")
        rsi         = summary.get("rsi14")
        change_pct  = summary.get("change_pct") or 0.0

        holding = next((h for h in portfolio if h["ticker"] == ticker), None)

        # ── 強制損切りチェック（スコア計算より先に判断）──
        if holding and holding["shares"] > 0:
            pnl_pct = (price / holding["avg_cost"] - 1) * 100
            if pnl_pct <= -10:
                decisions.append({
                    "ticker": ticker,
                    "action": "sell",
                    "reason": f"損切り（含み損{pnl_pct:.1f}%）",
                })
                continue

        # 指標が揃っていない場合はスキップ
        if ma5 is None or ma25 is None or rsi is None:
            decisions.append({
                "ticker": ticker,
                "action": "hold",
                "reason": "指標データ不足のため様子見",
            })
            continue

        # ── スコア計算 ──
        buy_score  = 0
        sell_score = 0
        buy_reasons  = []
        sell_reasons = []

        # トレンド判断（MA5とMA25の差の大きさで点数が変わる）
        ma_diff_pct = abs(ma5 - ma25) / ma25 * 100 if ma25 else 0
        trend_points = min(30, 10 + ma_diff_pct * 4)

        if ma5 > ma25:
            buy_score += trend_points
            buy_reasons.append(f"上昇トレンド(MA差{ma_diff_pct:.1f}%)")
        else:
            sell_score += trend_points
            sell_reasons.append(f"下降トレンド(MA差{ma_diff_pct:.1f}%)")

        # RSI判断（範囲を広めに設定して反応しやすくする）
        if rsi < 40:
            buy_score += 25
            buy_reasons.append(f"RSI売られすぎ({rsi:.1f})")
        elif rsi < 55:
            buy_score += 15
            buy_reasons.append(f"RSI低め({rsi:.1f})")
        elif rsi > 65:
            sell_score += 25
            sell_reasons.append(f"RSI買われすぎ({rsi:.1f})")
        elif rsi > 55:
            sell_score += 15
            sell_reasons.append(f"RSIやや高め({rsi:.1f})")

        # 当日の値動き
        if change_pct > 1.5:
            buy_score += 15
            buy_reasons.append(f"本日+{change_pct:.1f}%上昇")
        elif change_pct < -1.5:
            sell_score += 15
            sell_reasons.append(f"本日{change_pct:.1f}%下落")

        # 現金余裕ボーナス（買いの場合のみ）
        if cash_ratio >= 0.35:
            buy_score += 10

        # ── 判断 ──
        if buy_score >= 40 and buy_score > sell_score:
            reason = "、".join(buy_reasons[:2]) or "総合判断で買い"
            decisions.append({"ticker": ticker, "action": "buy", "reason": reason})

        elif sell_score >= 35 and sell_score > buy_score and holding:
            reason = "、".join(sell_reasons[:2]) or "総合判断で売り"
            decisions.append({"ticker": ticker, "action": "sell", "reason": reason})

        else:
            # hold の理由は買い・売りどちらが強いかで変える
            if buy_score > sell_score:
                reason = "、".join(buy_reasons[:1]) + "だが買いサイン弱め" if buy_reasons else "様子見"
            elif sell_score > buy_score:
                reason = "、".join(sell_reasons[:1]) + "だが売りサイン弱め" if sell_reasons else "様子見"
            else:
                reason = "様子見"
            decisions.append({"ticker": ticker, "action": "hold", "reason": reason})

    return decisions


def _execute_buy(ticker: str, company_name: str, shares: int, price: float,
                 reason: str, cash: float, total_assets: float) -> bool:
    """買い注文を実行する"""
    trade_amount = price * shares
    commission   = calc_commission(trade_amount)
    total_cost   = trade_amount + commission

    # 現金不足チェック
    if cash - total_cost < total_assets * MIN_CASH_RATIO:
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
    new_avg_cost = (
        (holding["avg_cost"] * holding["shares"] + trade_amount) / new_shares
        if holding else price
    )

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
    2. スコアリングエンジンで売買判断
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
    account      = get_account()
    portfolio    = get_portfolio()
    prices       = {s["ticker"]: s["current_price"] for s in stock_summaries}
    total_assets = calc_total_assets(account["cash"], portfolio, prices)

    print(f"💰 総資産: {total_assets:,.0f}円 / 現金: {account['cash']:,.0f}円")

    # スコアリングエンジンで判断
    print("🧠 スコアリングエンジンが相場を分析中...")
    decisions = _judge_all_stocks(account, portfolio, stock_summaries, prices)

    executed = []
    skipped  = []

    for decision in decisions:
        ticker  = decision.get("ticker")
        action  = decision.get("action", "hold")
        reason  = decision.get("reason", "")

        if ticker not in WATCHLIST:
            continue

        summary = next((s for s in stock_summaries if s["ticker"] == ticker), None)
        if not summary:
            continue

        company_name = summary["company_name"]
        price        = summary["current_price"]
        holding      = get_holding(ticker)

        # 最新の現金残高を取得（前の取引で変わっているため）
        current_cash = get_account()["cash"]

        print(f"  {company_name}: {action.upper()} - {reason}")

        if action == "buy":
            buy_shares = max(1, int(total_assets * MAX_SINGLE_TRADE_RATIO / price))
            success = _execute_buy(
                ticker, company_name, buy_shares, price,
                reason, current_cash, total_assets,
            )
            (executed if success else skipped).append(f"buy:{ticker}")

        elif action == "sell":
            if holding and holding["shares"] > 0:
                # 損切り（含み損-10%超え）の場合は全部売る、それ以外は半分売る
                pnl_pct = (price / holding["avg_cost"] - 1) * 100
                sell_shares = (
                    holding["shares"] if pnl_pct <= -10
                    else max(1, holding["shares"] // 2)
                )
                success = _execute_sell(
                    ticker, company_name, sell_shares, price, reason, current_cash
                )
                (executed if success else skipped).append(f"sell:{ticker}")
            else:
                print(f"  {company_name}: sell指示だが未保有のためスキップ")
                skipped.append(f"sell:{ticker}")

        # hold の場合は何もしない

    # 取引後の資産スナップショットを保存（グラフ用）
    account_after   = get_account()
    portfolio_after = get_portfolio()
    total_after     = calc_total_assets(account_after["cash"], portfolio_after, prices)
    stock_value     = total_after - account_after["cash"]
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
