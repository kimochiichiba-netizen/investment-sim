"""
清原式 × プロ仕様リスク管理 ハイブリッド投資エンジン

【改良ポイント】

■ トレーリングストップ（利益を守りながら伸ばす）
  - 購入後、価格が上がるたびに「ストップ価格」も引き上げる
  - 例: 1,000円で買った株が1,500円(+50%)になったら → ストップを1,350円(-10%)に設定
  - 通常は最高値から -15%、含み益+30%超えたら -10% に引き締める

■ テクニカル入力フィルター
  - RSI(相対力指数)が65以上（買われすぎ）の時は買わない
  - MA5(5日平均)がMA25(25日平均)を上回っている = 上昇トレンドなら優先

■ スコアベースのポジションサイズ
  - NC比率（ネットキャッシュ比率）が高いほど多く投資
  - 最大15%、最小5%の範囲で動的配分

■ 段階的利確
  - +50% 達成 → 半分売り（確実に利益を確定）
  - +100% 達成 → 残り全部売り（2倍達成！）
  - トレーリングストップに当たったら → 全部売り

■ 売り優先度
  1. 損切り（ストップ価格以下）
  2. 2倍達成（全部売り）
  3. +50% 達成（半分売り、1回のみ）
  4. スクリーニング条件から外れた
"""
import math
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
    update_trailing_stop,
    mark_partial_taken,
    get_trade_stats,
    recently_sold,
)
from fundamental_data import get_fundamental, get_current_price
from stock_universe import SMALL_CAP_UNIVERSE

try:
    from stock_data import get_stock_summary, get_prices_bulk
    HAS_STOCK_DATA = True
except ImportError:
    HAS_STOCK_DATA = False

# ── リスク管理パラメータ ──────────────────────────────────
MAX_HOLDINGS           = 10     # 最大保有銘柄数
MIN_POSITION_RATIO     = 0.05   # 最小投資比率（総資産の5%）
MAX_POSITION_RATIO     = 0.15   # 最大投資比率（総資産の15%）
MIN_CASH_RATIO         = 0.25   # 常に現金25%以上を維持（余裕を増やす）
COMMISSION_RATE        = 0.001  # 手数料 0.1%
MIN_COMMISSION         = 100    # 最低手数料 100円

# ── トレーリングストップ設定 ──────────────────────────────
TRAIL_INITIAL_PCT      = 0.92   # 初期ストップ: 最高値の 92%（= -8%）損切り素早く
TRAIL_TIGHT_PCT        = 0.94   # 引き締め後: 最高値の 94%（= -6%）利益をしっかり守る
TRAIL_TIGHTEN_TRIGGER  = 15.0   # 含み益が+15%を超えたら早めにストップを引き締める

# ── 利確設定 ────────────────────────────────────────────
PROFIT_TARGET_FULL     = 30.0   # 全部売り: +30%（確実に利益を取る）
PROFIT_TARGET_PARTIAL  = 15.0   # 半分売り: +15%（早めに半分確定）

# ── テクニカルフィルター ─────────────────────────────────
RSI_MAX_FOR_BUY        = 60     # 買い時のRSI上限（過熱気味の銘柄を避ける）


def calc_commission(amount: float) -> float:
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)


def calc_total_assets(cash: float, portfolio: List[Dict], prices: Dict[str, float]) -> float:
    stock_value = sum(
        h["shares"] * prices.get(h["ticker"], h["avg_cost"])
        for h in portfolio
    )
    return cash + stock_value


def _get_prices_for_portfolio(portfolio: List[Dict]) -> Dict[str, float]:
    """保有銘柄の現在価格を一括取得（速度優先）"""
    if not portfolio:
        return {}
    tickers = [h["ticker"] for h in portfolio]
    # 一括取得できれば使う（速い）
    if HAS_STOCK_DATA:
        try:
            prices = get_prices_bulk(tickers)
            result = {}
            for h in portfolio:
                p = prices.get(h["ticker"])
                result[h["ticker"]] = p if p else h["avg_cost"]
            return result
        except Exception:
            pass
    # フォールバック: 個別取得
    result = {}
    for h in portfolio:
        p = get_current_price(h["ticker"])
        result[h["ticker"]] = p if p else h["avg_cost"]
    return result


def _calc_trailing_stop(peak_price: float, pnl_pct: float) -> float:
    """
    トレーリングストップ価格を計算する。

    - 通常: 最高値 × 85%（-15%）
    - 含み益+30%超: 最高値 × 90%（-10%）→ 利益を守るために引き締め
    """
    if pnl_pct >= TRAIL_TIGHTEN_TRIGGER:
        return round(peak_price * TRAIL_TIGHT_PCT, 1)
    return round(peak_price * TRAIL_INITIAL_PCT, 1)


def _decide_sell(holding: Dict, current_price: float) -> Tuple[Optional[str], str]:
    """
    1銘柄の売り判断。

    返り値: ("full" | "half" | None, 理由)
    - "full"  → 全株売却
    - "half"  → 半分だけ売却（部分利確）
    - None    → 売らない（保有継続）
    """
    avg_cost = holding["avg_cost"]
    if avg_cost <= 0:
        return None, ""

    pnl_pct = (current_price / avg_cost - 1) * 100

    # 最高値とトレーリングストップを更新
    peak = max(holding.get("peak_price") or avg_cost, current_price)
    trail = _calc_trailing_stop(peak, pnl_pct)

    # DB のストップ価格も更新
    update_trailing_stop(holding["ticker"], peak, trail)

    # ① トレーリングストップに当たった（損失または利益の一部を守る）
    if current_price < trail:
        if pnl_pct >= 0:
            return "full", f"トレーリングストップ発動（最高値{peak:,.0f}円 → ストップ{trail:,.0f}円、含み益{pnl_pct:+.1f}%）"
        else:
            return "full", f"損切り発動（ストップ{trail:,.0f}円、含み損{pnl_pct:.1f}%）"

    # ② 2倍達成 → 全部利確
    if pnl_pct >= PROFIT_TARGET_FULL:
        return "full", f"🎉 2倍達成！ +{pnl_pct:.1f}% 全量利確"

    # ③ +50% 達成 → 半分利確（1回のみ）
    partial_taken = holding.get("partial_taken") or 0
    if pnl_pct >= PROFIT_TARGET_PARTIAL and not partial_taken and holding["shares"] >= 2:
        return "half", f"中間利確 +{pnl_pct:.1f}% 半分売却（残りは2倍まで保有）"

    # ④ スクリーニング条件から外れた
    screened_list = get_screened_stocks()
    if len(screened_list) > 0 and not is_screened(holding["ticker"]):
        return "full", "ファンダメンタル悪化（スクリーニング条件から外れた）"

    return None, ""


def _calc_position_size(total_assets: float, nc_ratio: Optional[float]) -> float:
    """
    スコアベースのポジションサイズ計算。

    NC比率が高い銘柄ほど多く投資する（清原式の確信度に応じた配分）。
    範囲: MIN_POSITION_RATIO ～ MAX_POSITION_RATIO
    """
    if nc_ratio is None:
        return MIN_POSITION_RATIO

    # NC比率 0.3 → 5%、1.0 → 10%、2.0以上 → 15%
    ratio = MIN_POSITION_RATIO + (MAX_POSITION_RATIO - MIN_POSITION_RATIO) * min(1.0, (nc_ratio - 0.3) / 1.7)
    ratio = max(MIN_POSITION_RATIO, min(MAX_POSITION_RATIO, ratio))
    return ratio


def run_sell_check() -> Dict:
    """
    【毎分実行】保有銘柄の売りシグナルチェック。

    トレーリングストップ・2倍達成・中間利確・ファンダメンタル悪化を確認し、
    条件を満たした銘柄を自動売却します。
    """
    portfolio = get_portfolio()
    if not portfolio:
        return {"status": "ok", "executed": []}

    prices    = _get_prices_for_portfolio(portfolio)
    executed  = []

    for holding in portfolio:
        ticker        = holding["ticker"]
        current_price = prices.get(ticker, holding["avg_cost"])

        # 無効な価格はスキップ
        if not current_price or current_price <= 0 or math.isnan(float(current_price)):
            continue

        sell_type, reason = _decide_sell(holding, current_price)
        if not sell_type:
            continue

        # 売る株数を決定
        if sell_type == "half":
            sell_shares = max(1, holding["shares"] // 2)
        else:
            sell_shares = holding["shares"]

        trade_amount = current_price * sell_shares
        commission   = calc_commission(trade_amount)
        proceeds     = trade_amount - commission

        # DB 更新
        if sell_shares >= holding["shares"]:
            delete_holding(ticker)
        else:
            remaining = holding["shares"] - sell_shares
            upsert_holding(ticker, holding["company_name"], remaining, holding["avg_cost"])
            mark_partial_taken(ticker)

        current_cash = get_account()["cash"]
        update_cash(current_cash + proceeds)
        save_trade(ticker, holding["company_name"], "sell",
                   sell_shares, current_price, trade_amount, commission, reason)

        pnl = (current_price - holding["avg_cost"]) * sell_shares
        print(f"✅ 売却: {holding['company_name']}({ticker}) {sell_shares}株 "
              f"@{current_price:,.0f}円 損益:{pnl:+,.0f}円 → {reason}")
        executed.append(f"sell:{ticker}")

    return {"status": "ok", "executed": executed}


def run_buy_execution() -> Dict:
    """
    【毎分実行】スクリーニング通過銘柄の購入チェック。

    保有10銘柄未満かつ現金に余裕がある場合、
    RSIフィルターをパスした銘柄を自動購入します。
    """
    screened  = get_screened_stocks()  # スコア降順（キャッシュ済みで高速）
    if not screened:
        return {"status": "ok", "executed": []}

    account  = get_account()
    portfolio = get_portfolio()

    held_tickers = {h["ticker"] for h in portfolio}
    if len(held_tickers) >= MAX_HOLDINGS:
        return {"status": "ok", "executed": [], "message": f"保有上限({MAX_HOLDINGS}銘柄)"}

    prices = _get_prices_for_portfolio(portfolio)
    # スクリーニング候補の価格も追加
    for s in screened:
        if s["ticker"] not in prices:
            p = get_current_price(s["ticker"])
            if p:
                prices[s["ticker"]] = p

    total_assets = calc_total_assets(account["cash"], portfolio, prices)
    current_cash = account["cash"]
    executed = []

    for candidate in screened:
        ticker = candidate["ticker"]

        if ticker in held_tickers:
            continue
        if len(held_tickers) >= MAX_HOLDINGS:
            break

        # ── クールダウンチェック（3日以内に売った銘柄は再購入しない）──
        # 売ってすぐ買い直すと手数料が二重にかかるため
        if recently_sold(ticker, days=3):
            print(f"  ⏸ {ticker} 直近3日以内に売却済み → クールダウン中のためスキップ")
            continue

        current_price = prices.get(ticker)
        # NaN・None・0 はすべてスキップ
        if not current_price or current_price <= 0 or math.isnan(current_price):
            continue

        # ── テクニカルフィルター（RSI確認）──
        rsi = None
        if HAS_STOCK_DATA:
            try:
                summary = get_stock_summary(ticker)
                if summary:
                    rsi = summary.get("rsi14")
                    if rsi and not math.isnan(rsi) and rsi > RSI_MAX_FOR_BUY:
                        print(f"  ⏸ {ticker} RSI{rsi:.0f} > {RSI_MAX_FOR_BUY}（高すぎ）スキップ")
                        continue
            except Exception:
                pass

        # ── ポジションサイズ計算 ─────────────────────────────
        nc_ratio      = candidate.get("net_cash_ratio")
        pos_ratio     = _calc_position_size(total_assets, nc_ratio)
        target_amount = total_assets * pos_ratio

        # NaN チェック（total_assets が NaN になることがある）
        if math.isnan(target_amount) or math.isnan(total_assets):
            print(f"  ⏸ {ticker} 資産計算エラー（NaN）スキップ")
            continue

        available_cash = current_cash - total_assets * MIN_CASH_RATIO
        if available_cash < current_price:
            print(f"  ⏸ {ticker} 使える現金不足（{available_cash:,.0f}円）スキップ")
            continue

        invest_amount = min(target_amount, available_cash)
        # NaN ガード（ゼロ除算・NaN を int() に渡さない）
        if invest_amount <= 0 or math.isnan(invest_amount):
            continue
        shares = max(1, int(invest_amount / current_price))

        trade_amount = current_price * shares
        commission   = calc_commission(trade_amount)
        total_cost   = trade_amount + commission

        if total_cost > current_cash - total_assets * MIN_CASH_RATIO:
            adj = available_cash * 0.99
            if adj <= 0 or math.isnan(adj):
                continue
            shares       = max(1, int(adj / current_price))
            trade_amount = current_price * shares
            commission   = calc_commission(trade_amount)
            total_cost   = trade_amount + commission

        if shares <= 0 or total_cost > current_cash:
            continue

        company_name = candidate.get("company_name") or SMALL_CAP_UNIVERSE.get(ticker, ticker)

        # 初期トレーリングストップ設定
        initial_trail = round(current_price * TRAIL_INITIAL_PCT, 1)

        fdata = get_fundamental(ticker)

        # DB 更新
        upsert_holding(
            ticker, company_name, shares, current_price,
            buy_per=fdata.get("per")             if fdata else None,
            buy_pbr=fdata.get("pbr")             if fdata else None,
            buy_net_cash_ratio=nc_ratio,
            target_price=round(current_price * 2, 1),
            catalyst_notes=f"NC比率:{nc_ratio:.2f}" if nc_ratio else None,
            peak_price=current_price,
            trailing_stop=initial_trail,
        )
        current_cash_updated = get_account()["cash"]
        update_cash(current_cash_updated - total_cost)
        current_cash = current_cash_updated - total_cost

        reason = (f"清原式スクリーニング通過 NC比率:{nc_ratio:.2f}"
                  f"{f' RSI:{rsi:.0f}' if rsi else ''}"
                  f" 配分:{pos_ratio*100:.0f}%")
        save_trade(ticker, company_name, "buy",
                   shares, current_price, trade_amount, commission, reason)

        held_tickers.add(ticker)
        executed.append(f"buy:{ticker}")
        print(f"✅ 購入: {company_name}({ticker}) {shares}株 @{current_price:,.0f}円 "
              f"ストップ:{initial_trail:,.0f}円 目標:{round(current_price*2,1):,.0f}円")

    return {"status": "ok", "executed": executed}


def run_fundamental_trading() -> Dict:
    """
    毎分実行されるメイン関数。
    売りチェック → 買い付けチェック → 資産スナップショット保存。
    """
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now_str}] 📊 売買チェック開始", end=" ", flush=True)

    sell_result = run_sell_check()
    buy_result  = run_buy_execution()

    all_executed = sell_result.get("executed", []) + buy_result.get("executed", [])

    # 取引があった時だけ詳細ログ
    if all_executed:
        print(f"→ 売り:{len(sell_result['executed'])}件 買い:{len(buy_result['executed'])}件")
    else:
        print("→ 取引なし")

    # 資産スナップショット保存（グラフ用）
    account_after   = get_account()
    portfolio_after = get_portfolio()
    prices_after    = _get_prices_for_portfolio(portfolio_after)
    total_after     = calc_total_assets(account_after["cash"], portfolio_after, prices_after)
    save_asset_snapshot(total_after, account_after["cash"], total_after - account_after["cash"])

    pnl = total_after - account_after["initial_capital"]
    return {
        "status":          "success",
        "executed_at":     datetime.now().isoformat(),
        "total_assets":    round(total_after, 0),
        "pnl":             round(pnl, 0),
        "executed_trades": all_executed,
        "sell_count":      len(sell_result.get("executed", [])),
        "buy_count":       len(buy_result.get("executed", [])),
    }
