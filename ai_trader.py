"""
AI売買判断エンジン
Claude APIを使って日本株の売買判断を行います

保守的なリスク管理:
- 1回の取引は総資産の5%以下
- 1銘柄への投資上限は総資産の20%
- 現金は常に総資産の30%以上を維持
- 手数料: 取引額の0.1%（最低100円）
"""
import os
import json
from typing import List, Dict, Tuple
from datetime import datetime

import anthropic

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
from stock_data import get_all_watchlist_summaries, get_current_price, WATCHLIST

# Claude AIクライアント
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# リスク管理パラメータ
MAX_SINGLE_TRADE_RATIO = 0.05   # 1回の取引は総資産の5%まで
MAX_SINGLE_STOCK_RATIO = 0.20   # 1銘柄は総資産の20%まで
MIN_CASH_RATIO = 0.30            # 現金は総資産の30%以上を維持
COMMISSION_RATE = 0.001          # 手数料 0.1%
MIN_COMMISSION = 100             # 最低手数料 100円


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


def _build_prompt(
    account: Dict,
    portfolio: List[Dict],
    stock_summaries: List[Dict],
    total_assets: float,
) -> str:
    """Claude APIに渡すプロンプトを構築する"""

    # 現在のポートフォリオ情報を整形
    portfolio_text = "（現在保有株なし）"
    if portfolio:
        lines = []
        for h in portfolio:
            ticker = h["ticker"]
            summary = next((s for s in stock_summaries if s["ticker"] == ticker), None)
            current_price = summary["current_price"] if summary else h["avg_cost"]
            unrealized = (current_price - h["avg_cost"]) * h["shares"]
            unrealized_pct = (current_price / h["avg_cost"] - 1) * 100
            lines.append(
                f"  - {h['company_name']}({ticker}): "
                f"{h['shares']}株 @平均{h['avg_cost']:,.0f}円, "
                f"現在{current_price:,.0f}円, "
                f"含み損益 {unrealized:+,.0f}円 ({unrealized_pct:+.1f}%)"
            )
        portfolio_text = "\n".join(lines)

    # 銘柄データを整形
    stock_text_parts = []
    for s in stock_summaries:
        ma_cross = ""
        if s["ma5"] and s["ma25"]:
            if s["ma5"] > s["ma25"]:
                ma_cross = "【ゴールデンクロス: 上昇トレンド】"
            else:
                ma_cross = "【デッドクロス: 下降トレンド】"
        rsi_signal = ""
        if s["rsi14"]:
            if s["rsi14"] >= 70:
                rsi_signal = "【RSI過熱圏: 売りシグナル】"
            elif s["rsi14"] <= 30:
                rsi_signal = "【RSI売られすぎ: 買いシグナル】"

        stock_text_parts.append(
            f"■ {s['company_name']}({s['ticker']})\n"
            f"  現在値: {s['current_price']:,}円 (前日比 {s['change_pct']:+.2f}%)\n"
            f"  MA5: {s['ma5']}円 / MA25: {s['ma25']}円 {ma_cross}\n"
            f"  RSI(14): {s['rsi14']} {rsi_signal}\n"
            f"  出来高: {s['volume']:,}"
        )

    stocks_text = "\n\n".join(stock_text_parts)

    max_single_trade = total_assets * MAX_SINGLE_TRADE_RATIO
    min_cash = total_assets * MIN_CASH_RATIO
    current_cash = account["cash"]

    return f"""あなたは日本株投資のプロのトレーダーです。
以下の市場データとポートフォリオ状況を分析し、売買判断を行ってください。

【現在の資産状況】
- 総資産: {total_assets:,.0f}円
- 現金残高: {current_cash:,.0f}円（必ず {min_cash:,.0f}円以上を維持すること）
- 現在のポートフォリオ:
{portfolio_text}

【リスク管理ルール（厳守）】
- 1回の取引の上限: {max_single_trade:,.0f}円（総資産の5%）
- 1銘柄への投資上限: {total_assets * MAX_SINGLE_STOCK_RATIO:,.0f}円（総資産の20%）
- 現金の最低保有額: {min_cash:,.0f}円（総資産の30%）
- 手数料は取引額の0.1%（最低100円）かかります

【監視銘柄の市場データ（本日時点）】
{stocks_text}

【指示】
上記データを分析し、以下のJSON形式のみで回答してください。
余計なテキストは一切含めないでください。

[
  {{"action": "buy", "ticker": "7203.T", "shares": 10, "reason": "判断理由を日本語で簡潔に"}},
  {{"action": "sell", "ticker": "6758.T", "shares": 5, "reason": "判断理由を日本語で簡潔に"}},
  {{"action": "hold", "ticker": "9984.T", "shares": 0, "reason": "判断理由を日本語で簡潔に"}}
]

【注意事項】
- 必ず全監視銘柄についてbuy/sell/holdのいずれかを返すこと
- buyの場合、現金残高と1回の取引上限を必ず確認してから株数を決める
- sellの場合、実際の保有株数を超えて売ることはできない
- 保有していない銘柄はsellにしないこと
- JSON以外のテキストは絶対に含めないこと
"""


def execute_trade_decision(decision: Dict, stock_summaries: List[Dict], total_assets: float) -> bool:
    """
    AIの判断を実際に実行する
    True: 実行成功, False: スキップ（ルール違反など）
    """
    action = decision.get("action", "hold")
    ticker = decision.get("ticker", "")
    shares = int(decision.get("shares", 0))
    reason = decision.get("reason", "")
    company_name = WATCHLIST.get(ticker, ticker)

    if action == "hold" or shares <= 0:
        return False

    summary = next((s for s in stock_summaries if s["ticker"] == ticker), None)
    if not summary:
        print(f"⚠️  {ticker} の株価データがないためスキップ")
        return False

    price = summary["current_price"]
    account = get_account()
    cash = account["cash"]

    if action == "buy":
        trade_amount = price * shares
        commission = calc_commission(trade_amount)
        total_cost = trade_amount + commission

        # リスク管理チェック
        if total_cost > total_assets * MAX_SINGLE_TRADE_RATIO:
            # 取引上限を超えている場合は株数を調整
            max_amount = total_assets * MAX_SINGLE_TRADE_RATIO
            shares = max(1, int(max_amount / price))
            trade_amount = price * shares
            commission = calc_commission(trade_amount)
            total_cost = trade_amount + commission
            print(f"⚠️  {ticker} 取引上限のため {shares}株に調整")

        if cash - total_cost < total_assets * MIN_CASH_RATIO:
            print(f"⚠️  {ticker} 現金不足のためスキップ (必要: {total_cost:,.0f}円)")
            return False

        # 既存保有分と合算して上限チェック
        holding = get_holding(ticker)
        current_holding_value = (holding["shares"] * price) if holding else 0
        new_holding_value = current_holding_value + trade_amount
        if new_holding_value > total_assets * MAX_SINGLE_STOCK_RATIO:
            print(f"⚠️  {ticker} 1銘柄上限超えのためスキップ")
            return False

        # 購入実行
        new_shares = shares + (holding["shares"] if holding else 0)
        if holding:
            new_avg_cost = (holding["avg_cost"] * holding["shares"] + trade_amount) / new_shares
        else:
            new_avg_cost = price

        upsert_holding(ticker, company_name, new_shares, new_avg_cost)
        update_cash(cash - total_cost)
        save_trade(ticker, company_name, "buy", shares, price, trade_amount, commission, reason)
        print(f"✅ 買い: {company_name}({ticker}) {shares}株 @{price:,}円 手数料{commission:.0f}円")
        return True

    elif action == "sell":
        holding = get_holding(ticker)
        if not holding or holding["shares"] < shares:
            print(f"⚠️  {ticker} 保有株不足のためスキップ")
            return False

        trade_amount = price * shares
        commission = calc_commission(trade_amount)
        proceeds = trade_amount - commission

        # 売却実行
        new_shares = holding["shares"] - shares
        if new_shares <= 0:
            delete_holding(ticker)
        else:
            upsert_holding(ticker, company_name, new_shares, holding["avg_cost"])

        update_cash(cash + proceeds)
        save_trade(ticker, company_name, "sell", shares, price, trade_amount, commission, reason)
        print(f"✅ 売り: {company_name}({ticker}) {shares}株 @{price:,}円 手数料{commission:.0f}円")
        return True

    return False


def run_ai_trading() -> Dict:
    """
    AIによる自動取引を実行する（メイン関数）

    1. 全銘柄の市場データを取得
    2. Claude APIに判断を依頼
    3. 判断を実行
    4. 結果を返す
    """
    print(f"\n{'='*50}")
    print(f"🤖 AI取引開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    # 市場データ取得
    print("📊 市場データを取得中...")
    stock_summaries = get_all_watchlist_summaries()
    if not stock_summaries:
        return {"status": "error", "message": "市場データの取得に失敗しました"}

    # 現在の資産状況
    account = get_account()
    portfolio = get_portfolio()

    # 現在価格で総資産を計算
    prices = {s["ticker"]: s["current_price"] for s in stock_summaries}
    total_assets = calc_total_assets(account["cash"], portfolio, prices)

    print(f"💰 総資産: {total_assets:,.0f}円 / 現金: {account['cash']:,.0f}円")

    # Claude APIで判断
    print("🧠 Claude AIが市場を分析中...")
    try:
        prompt = _build_prompt(account, portfolio, stock_summaries, total_assets)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        print(f"🤖 AI判断: {raw[:200]}...")
        decisions = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ AI判断のJSON解析エラー: {e}")
        return {"status": "error", "message": f"AI判断の解析に失敗: {e}"}
    except Exception as e:
        print(f"❌ Claude APIエラー: {e}")
        return {"status": "error", "message": f"Claude APIエラー: {e}"}

    # 判断を実行
    executed = []
    skipped = []
    for decision in decisions:
        action = decision.get("action", "hold")
        ticker = decision.get("ticker", "")
        if action != "hold":
            success = execute_trade_decision(decision, stock_summaries, total_assets)
            if success:
                executed.append(f"{action}:{ticker}")
            else:
                skipped.append(f"{action}:{ticker}")

    # 資産スナップショットを保存（グラフ用）
    account_after = get_account()
    portfolio_after = get_portfolio()
    total_after = calc_total_assets(account_after["cash"], portfolio_after, prices)
    stock_value = total_after - account_after["cash"]
    save_asset_snapshot(total_after, account_after["cash"], stock_value)

    result = {
        "status": "success",
        "executed_at": datetime.now().isoformat(),
        "total_assets_before": round(total_assets, 0),
        "total_assets_after": round(total_after, 0),
        "pnl": round(total_after - account["initial_capital"], 0),
        "executed_trades": executed,
        "skipped_trades": skipped,
        "ai_decisions_count": len(decisions),
    }
    print(f"🏁 AI取引完了: {len(executed)}件実行 / {len(skipped)}件スキップ")
    print(f"📈 総資産: {total_assets:,.0f}円 → {total_after:,.0f}円")
    return result
