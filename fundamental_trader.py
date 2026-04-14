"""
清原式ファンダメンタル投資エンジン

【買いルール（清原式）】
- スクリーニング通過銘柄のうち、まだ保有していないものを買う
- 保有銘柄は最大10銘柄まで（分散投資）
- 1銘柄への投資額 = 総資産 ÷ 10（均等配分）
- 現金が30%以上あることが条件

【売りルール（清原式の核心）】
- 含み益 +100% 達成（2倍）→ 利確
- 含み損 -30% 以下 → 損切り（ファンダメンタル悪化と判断）
- スクリーニング条件から外れた → 撤退
※ +30%程度では「絶対に売らない」のが清原式の核心！
"""
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from database import (
    get_account,
    get_portfolio,
    get_holding,
    upsert_holding,
    delete_holding,
    save_trade,
    update_cash,
    save_asset_snapshot,
    get_screened_stocks,
    is_screened,
)
from fundamental_data import get_fundamental, get_current_price
from stock_universe import SMALL_CAP_UNIVERSE

# ── リスク管理パラメータ ──────────────────────────────────
MAX_HOLDINGS       = 10      # 最大保有銘柄数
TARGET_ALLOCATION  = 0.10    # 1銘柄あたりの目標配分（総資産の10%）
MIN_CASH_RATIO     = 0.20    # 現金は常に総資産の20%以上を維持
COMMISSION_RATE    = 0.001   # 手数料 0.1%
MIN_COMMISSION     = 100     # 最低手数料 100円

# ── 売り判断の閾値 ──────────────────────────────────────
PROFIT_TARGET_PCT  = 100.0   # 利確ライン: +100%（2倍）
STOP_LOSS_PCT      = -30.0   # 損切りライン: -30%


def calc_commission(amount: float) -> float:
    """手数料を計算する（0.1%、最低100円）"""
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)


def calc_total_assets(cash: float, portfolio: List[Dict], prices: Dict[str, float]) -> float:
    """総資産 = 現金 + 保有株の時価評価額"""
    stock_value = sum(
        h["shares"] * prices.get(h["ticker"], h["avg_cost"])
        for h in portfolio
    )
    return cash + stock_value


def _get_prices_for_portfolio(portfolio: List[Dict]) -> Dict[str, float]:
    """保有銘柄の現在価格を一括取得"""
    prices = {}
    for h in portfolio:
        price = get_current_price(h["ticker"])
        if price:
            prices[h["ticker"]] = price
        else:
            prices[h["ticker"]] = h["avg_cost"]  # 取得できなければ取得価格で代用
    return prices


def _decide_sell(holding: Dict, current_price: float) -> Tuple[bool, str]:
    """
    1銘柄について売り判断を行う。

    返り値: (売るかどうか, 理由)
    """
    avg_cost = holding["avg_cost"]
    if avg_cost <= 0:
        return False, ""

    pnl_pct = (current_price / avg_cost - 1) * 100

    # ① 含み益 +100% 達成 → 利確！
    if pnl_pct >= PROFIT_TARGET_PCT:
        return True, f"目標達成！含み益+{pnl_pct:.1f}%（2倍達成）"

    # ② 含み損 -30% 以下 → 損切り
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"損切り（含み損{pnl_pct:.1f}%）"

    # ③ スクリーニング条件から外れた → 撤退
    if not is_screened(holding["ticker"]):
        # スクリーニング結果がある場合のみチェック（空の場合は売らない）
        screened = get_screened_stocks()
        if len(screened) > 0:  # スクリーニングが実行済みの場合のみ
            return True, "スクリーニング条件から外れた（ファンダメンタル悪化）"

    return False, ""


def run_sell_check() -> Dict:
    """
    【毎分実行】保有銘柄の売りシグナルをチェックして実行する。

    2倍達成・損切り・ファンダメンタル悪化のいずれかで自動売却。
    """
    print(f"\n📡 保有銘柄の売りチェック中... ({datetime.now().strftime('%H:%M')})")

    account   = get_account()
    portfolio = get_portfolio()

    if not portfolio:
        print("   保有銘柄なし")
        return {"status": "ok", "executed": [], "message": "保有銘柄なし"}

    prices = _get_prices_for_portfolio(portfolio)
    total_assets = calc_total_assets(account["cash"], portfolio, prices)

    executed = []

    for holding in portfolio:
        ticker = holding["ticker"]
        current_price = prices.get(ticker, holding["avg_cost"])

        should_sell, reason = _decide_sell(holding, current_price)

        if should_sell:
            # 全株売却
            shares = holding["shares"]
            trade_amount = current_price * shares
            commission   = calc_commission(trade_amount)
            proceeds     = trade_amount - commission

            # DB 更新
            delete_holding(ticker)
            current_cash = get_account()["cash"]
            update_cash(current_cash + proceeds)
            save_trade(
                ticker, holding["company_name"], "sell",
                shares, current_price, trade_amount, commission, reason
            )
            print(f"✅ 売却: {holding['company_name']}({ticker}) "
                  f"{shares}株 @{current_price:,.0f}円 → {reason}")
            executed.append(f"sell:{ticker}")

    # 資産スナップショット保存
    account_after   = get_account()
    portfolio_after = get_portfolio()
    prices_after    = _get_prices_for_portfolio(portfolio_after)
    total_after     = calc_total_assets(account_after["cash"], portfolio_after, prices_after)
    save_asset_snapshot(total_after, account_after["cash"], total_after - account_after["cash"])

    return {"status": "ok", "executed": executed}


def run_buy_execution() -> Dict:
    """
    【毎朝7時 or 手動実行】スクリーニング通過銘柄を購入する。

    - 保有10銘柄未満のスロットに、スクリーニング上位銘柄を買う
    - 1銘柄あたり「総資産 ÷ 10」を目安に買い付ける
    """
    print(f"\n💰 買い付けチェックを開始... ({datetime.now().strftime('%H:%M')})")

    account      = get_account()
    portfolio    = get_portfolio()
    screened     = get_screened_stocks()  # スコア降順

    if not screened:
        print("   スクリーニング通過銘柄なし（先にスクリーニングを実行してください）")
        return {"status": "ok", "executed": [], "message": "スクリーニング通過銘柄なし"}

    # 現在の価格取得（保有銘柄）
    prices = _get_prices_for_portfolio(portfolio)
    # スクリーニング通過銘柄の価格も取得
    holding_tickers = {h["ticker"] for h in portfolio}
    for s in screened:
        if s["ticker"] not in prices:
            p = get_current_price(s["ticker"])
            if p:
                prices[s["ticker"]] = p

    total_assets = calc_total_assets(account["cash"], portfolio, prices)
    current_cash = account["cash"]
    n_holdings   = len(portfolio)

    print(f"   総資産: {total_assets:,.0f}円 / 現金: {current_cash:,.0f}円 / 保有: {n_holdings}/{MAX_HOLDINGS}銘柄")

    executed = []

    for candidate in screened:
        ticker = candidate["ticker"]

        # すでに保有中 or 最大保有数に達したらスキップ
        if ticker in holding_tickers:
            continue
        if n_holdings >= MAX_HOLDINGS:
            print(f"   保有上限({MAX_HOLDINGS}銘柄)に達したため終了")
            break

        current_price = prices.get(ticker)
        if not current_price or current_price <= 0:
            print(f"   {ticker} 価格取得不可のためスキップ")
            continue

        # 1銘柄あたりの目標投資額
        target_amount = total_assets * TARGET_ALLOCATION

        # 現金制約チェック（現金20%は残す）
        available_cash = current_cash - total_assets * MIN_CASH_RATIO
        if available_cash < current_price:
            print(f"   {ticker} 使える現金不足のためスキップ（使える残高: {available_cash:,.0f}円）")
            continue

        invest_amount = min(target_amount, available_cash)
        shares = max(1, int(invest_amount / current_price))

        trade_amount = current_price * shares
        commission   = calc_commission(trade_amount)
        total_cost   = trade_amount + commission

        # 最終現金チェック
        if current_cash - total_cost < total_assets * MIN_CASH_RATIO:
            shares = max(1, int(available_cash / (current_price * (1 + COMMISSION_RATE))))
            trade_amount = current_price * shares
            commission   = calc_commission(trade_amount)
            total_cost   = trade_amount + commission

        if shares <= 0 or total_cost > current_cash:
            print(f"   {ticker} 資金不足のためスキップ")
            continue

        # 財務データを取得して買い付け時の指標を記録
        fdata = get_fundamental(ticker)

        company_name = candidate.get("company_name") or SMALL_CAP_UNIVERSE.get(ticker, ticker)

        # DB 更新
        upsert_holding(
            ticker, company_name, shares, current_price,
            buy_per=fdata.get("per") if fdata else None,
            buy_pbr=fdata.get("pbr") if fdata else None,
            buy_net_cash_ratio=fdata.get("net_cash_ratio") if fdata else None,
            target_price=round(current_price * 2, 1),  # 2倍を目標価格に設定
            catalyst_notes=f"NC比率:{candidate.get('net_cash_ratio', 'N/A')}",
        )
        current_cash = get_account()["cash"]
        update_cash(current_cash - total_cost)
        current_cash = current_cash - total_cost

        save_trade(
            ticker, company_name, "buy",
            shares, current_price, trade_amount, commission,
            f"清原式スクリーニング通過（NC比率:{candidate.get('net_cash_ratio', 'N/A')}）"
        )

        n_holdings += 1
        holding_tickers.add(ticker)
        executed.append(f"buy:{ticker}")
        print(f"✅ 購入: {company_name}({ticker}) {shares}株 @{current_price:,.0f}円 "
              f"手数料:{commission:.0f}円 目標:{round(current_price*2, 1):,.0f}円")

    # 資産スナップショット保存
    account_after   = get_account()
    portfolio_after = get_portfolio()
    prices_after    = _get_prices_for_portfolio(portfolio_after)
    total_after     = calc_total_assets(account_after["cash"], portfolio_after, prices_after)
    save_asset_snapshot(total_after, account_after["cash"], total_after - account_after["cash"])

    print(f"💰 買い付け完了: {len(executed)}件")
    return {"status": "ok", "executed": executed, "total_assets": round(total_after, 0)}


def run_fundamental_trading() -> Dict:
    """
    売りチェック + 買い付けを一括実行するメイン関数。
    手動で「AI取引を実行」ボタンを押したときに呼ばれます。
    """
    print(f"\n{'='*50}")
    print(f"📊 清原式自動売買 開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    sell_result = run_sell_check()
    buy_result  = run_buy_execution()

    account_final   = get_account()
    portfolio_final = get_portfolio()
    prices_final    = _get_prices_for_portfolio(portfolio_final)
    total_final     = calc_total_assets(account_final["cash"], portfolio_final, prices_final)
    pnl             = total_final - account_final["initial_capital"]

    all_executed = sell_result.get("executed", []) + buy_result.get("executed", [])

    print(f"🏁 完了: 売り{len(sell_result.get('executed',[]))}件 / "
          f"買い{len(buy_result.get('executed',[]))}件")
    print(f"📈 総資産: {total_final:,.0f}円 (損益: {pnl:+,.0f}円)")

    return {
        "status":           "success",
        "executed_at":      datetime.now().isoformat(),
        "total_assets":     round(total_final, 0),
        "pnl":              round(pnl, 0),
        "executed_trades":  all_executed,
        "sell_count":       len(sell_result.get("executed", [])),
        "buy_count":        len(buy_result.get("executed", [])),
    }
