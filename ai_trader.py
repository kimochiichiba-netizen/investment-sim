"""
自動売買エンジン（Claude AI版）
Claude APIが相場データを見て、売買判断を行います。

【取引ルール（AIへの指示として渡す内容）】
- ゴールデンクロス（MA5 > MA25）は上昇サイン
- RSI14が30以下は「売られすぎ」= 買いチャンス
- RSI14が70以上は「買われすぎ」= 売りチャンス
- 含み損が -10% 超えたら損切り推奨

【リスク管理（プログラムで強制するルール）】
- 1回の取引は総資産の5%以下
- 1銘柄への投資上限は総資産の20%
- 現金は常に総資産の30%以上を維持
- 手数料: 取引額の0.1%（最低100円）
"""
import os
import json
import re
from datetime import datetime
from typing import List, Dict, Optional

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
from stock_data import get_all_watchlist_summaries, WATCHLIST

# リスク管理パラメータ
MAX_SINGLE_TRADE_RATIO = 0.05   # 1回の取引は総資産の5%まで
MAX_SINGLE_STOCK_RATIO = 0.20   # 1銘柄は総資産の20%まで
MIN_CASH_RATIO         = 0.30   # 現金は総資産の30%以上を維持
COMMISSION_RATE        = 0.001  # 手数料 0.1%
MIN_COMMISSION         = 100    # 最低手数料 100円

# Anthropicクライアント（遅延初期化）
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    """Anthropicクライアントを取得（初回のみ初期化）"""
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY が .env に設定されていません")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


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


def _ask_claude(account: Dict, portfolio: List[Dict],
                stock_summaries: List[Dict], prices: Dict[str, float]) -> List[Dict]:
    """
    Claude AIに相場分析と売買判断を依頼する

    返り値:
        [{"ticker": "7203.T", "action": "buy"/"sell"/"hold", "reason": "理由"}]
    """
    total_assets = calc_total_assets(account["cash"], portfolio, prices)

    # 保有銘柄の情報を整形
    holdings_info = []
    for h in portfolio:
        price = prices.get(h["ticker"], h["avg_cost"])
        pnl_pct = (price / h["avg_cost"] - 1) * 100 if h["avg_cost"] else 0
        holdings_info.append({
            "ticker": h["ticker"],
            "会社名": h["company_name"],
            "保有株数": h["shares"],
            "取得単価": h["avg_cost"],
            "現在値": price,
            "含み損益率": f"{pnl_pct:+.1f}%",
        })

    # 市場データを整形
    market_data = []
    for s in stock_summaries:
        market_data.append({
            "ticker": s["ticker"],
            "会社名": s["company_name"],
            "現在値": s["current_price"],
            "前日比": f"{s.get('change_pct', 0):+.2f}%",
            "MA5": s.get("ma5"),
            "MA25": s.get("ma25"),
            "RSI14": s.get("rsi14"),
        })

    prompt = f"""あなたは日本株の自動売買AIです。
以下の情報をもとに、各銘柄の売買判断をしてください。

【現在のポートフォリオ】
- 総資産: {total_assets:,.0f}円
- 現金: {account['cash']:,.0f}円（現金比率: {account['cash']/total_assets*100:.1f}%）

【保有中の銘柄】
{json.dumps(holdings_info, ensure_ascii=False, indent=2)}

【市場データ（監視銘柄10銘柄）】
{json.dumps(market_data, ensure_ascii=False, indent=2)}

【リスク管理ルール（必ず守ること）】
- 現金比率が30%を切る場合は買わない
- 1銘柄の保有上限は総資産の20%まで
- 含み損が-10%を超えた銘柄は売る（損切り）

【テクニカル指標の読み方】
- MA5 > MA25: 上昇トレンド（買いサイン）
- MA5 < MA25: 下降トレンド（売りサイン）
- RSI14 < 30: 売られすぎ（反発しやすい = 買いチャンス）
- RSI14 > 70: 買われすぎ（反落しやすい = 売りチャンス）

各銘柄について判断し、**JSONのみ**を返してください（説明文・コードブロック不要）:
[
  {{"ticker": "7203.T", "action": "buy", "reason": "30文字以内の理由"}},
  {{"ticker": "6758.T", "action": "hold", "reason": "30文字以内の理由"}},
  ...
]
"""

    client = _get_client()
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    # マークダウンコードブロックが含まれていた場合は除去
    if "```" in response_text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
        if match:
            response_text = match.group(1).strip()

    decisions = json.loads(response_text)
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
    AIによる自動取引を実行するメイン関数

    1. 全銘柄の市場データ（株価・テクニカル指標）を取得
    2. Claude AIに判断を依頼
    3. 判断を実行
    4. 結果を返す
    """
    print(f"\n{'='*50}")
    print(f"🤖 AI自動取引開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

    # Claude AIに判断を依頼
    print("🧠 Claude AIが相場を分析中...")
    try:
        decisions = _ask_claude(account, portfolio, stock_summaries, prices)
    except Exception as e:
        print(f"❌ Claude API エラー: {e}")
        return {"status": "error", "message": f"Claude APIエラー: {e}"}

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
