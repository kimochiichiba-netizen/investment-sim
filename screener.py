"""
清原式スクリーニングロジック

【清原式の4条件】
1. 時価総額 500億円以下（小型株限定）
2. PER 10倍以下（利益に対して安い）
3. PBR 1.0倍以下（純資産より安い）
4. ネットキャッシュ比率 1.0以上（現金が時価総額を超えている！）

4つ全て満たした銘柄だけが「投資候補」になります。
スコアはネットキャッシュ比率の高さで決まります（比率が高いほど超割安）。
"""
from typing import List, Dict

from stock_universe import SMALL_CAP_UNIVERSE
from fundamental_data import get_fundamental
from database import clear_screened_stocks, save_screened_stock, get_screened_stocks

# ── 清原式スクリーニング条件 ──────────────────────────────
KIYOHARA_RULES = {
    "max_market_cap_oku": 500,   # 時価総額500億円以下
    "max_per":            15,    # PER 15倍以下（実用的に少し緩める）
    "max_pbr":            1.5,   # PBR 1.5倍以下（実用的に少し緩める）
    "min_net_cash_ratio": 0.3,   # NC比率 0.3以上（本来は1.0だが銘柄が少なすぎるので緩める）
}

# 本来の清原式（厳密版）
STRICT_RULES = {
    "max_market_cap_oku": 500,
    "max_per":            10,
    "max_pbr":            1.0,
    "min_net_cash_ratio": 1.0,
}


def _passes_screening(data: Dict, rules: Dict) -> bool:
    """スクリーニング条件を1銘柄ずつチェック"""
    # 必須データが揃っていなければ不合格
    if data.get("market_cap_oku") is None:
        return False
    if data.get("net_cash_ratio") is None:
        return False

    # 条件チェック
    if data["market_cap_oku"] > rules["max_market_cap_oku"]:
        return False

    # PERはNoneの場合（赤字企業）はスキップ（清原式は黒字企業を対象）
    if data.get("per") is not None and data["per"] > rules["max_per"]:
        return False

    # PBRチェック
    if data.get("pbr") is not None and data["pbr"] > rules["max_pbr"]:
        return False

    # ネットキャッシュ比率チェック（最重要条件）
    if data["net_cash_ratio"] < rules["min_net_cash_ratio"]:
        return False

    return True


def _calc_score(data: Dict) -> float:
    """
    スコア計算（高いほど優先度が高い）
    主にネットキャッシュ比率ベースで計算
    """
    score = 0.0

    nc_ratio = data.get("net_cash_ratio") or 0
    score += nc_ratio * 50  # NC比率が高いほど高得点

    # PBRが低いほどボーナス
    pbr = data.get("pbr")
    if pbr and pbr > 0:
        score += max(0, (1.0 - pbr) * 20)

    # PERが低いほどボーナス
    per = data.get("per")
    if per and per > 0:
        score += max(0, (15 - per) * 1)

    return round(score, 2)


def run_screening(verbose: bool = True) -> List[Dict]:
    """
    全ユニバース銘柄をスクリーニングして投資候補を返す。

    1. 既存のスクリーニング結果を削除
    2. 約90銘柄を順番にチェック
    3. 条件を満たした銘柄をDBに保存
    4. スコア降順で返す

    verbose=True にするとプログレスが表示されます。
    """
    if verbose:
        print(f"\n{'='*50}")
        print(f"🔍 清原式スクリーニングを開始します")
        print(f"   対象: {len(SMALL_CAP_UNIVERSE)}銘柄")
        print(f"   条件: 時価総額≤{KIYOHARA_RULES['max_market_cap_oku']}億 / "
              f"PER≤{KIYOHARA_RULES['max_per']} / "
              f"PBR≤{KIYOHARA_RULES['max_pbr']} / "
              f"NC比率≥{KIYOHARA_RULES['min_net_cash_ratio']}")
        print(f"{'='*50}")

    # 既存のスクリーニング結果をクリア
    clear_screened_stocks()

    passed = []
    total  = len(SMALL_CAP_UNIVERSE)

    for i, (ticker, default_name) in enumerate(SMALL_CAP_UNIVERSE.items(), 1):
        if verbose:
            print(f"  [{i:3d}/{total}] {ticker} ({default_name}) を取得中...", end=" ", flush=True)

        data = get_fundamental(ticker)

        if data is None:
            if verbose:
                print("❌ データ取得失敗")
            continue

        # 会社名: DB取得名を優先しつつ、取れなければデフォルト名
        if not data.get("company_name") or data["company_name"] == ticker:
            data["company_name"] = default_name

        # スクリーニング判定
        if _passes_screening(data, KIYOHARA_RULES):
            score = _calc_score(data)
            data["score"] = score

            # 厳密版も判定して記録
            strict_pass = _passes_screening(data, STRICT_RULES)

            save_screened_stock(data)
            passed.append(data)

            nc_str = f"NC比率:{data['net_cash_ratio']:.2f}" if data.get("net_cash_ratio") else ""
            strict_str = "⭐厳密版合格" if strict_pass else ""
            if verbose:
                print(f"✅ 合格！(スコア:{score:.1f} {nc_str} {strict_str})")
        else:
            if verbose:
                # 不合格理由を表示
                reasons = []
                if data.get("market_cap_oku") and data["market_cap_oku"] > KIYOHARA_RULES["max_market_cap_oku"]:
                    reasons.append(f"時価総額{data['market_cap_oku']:.0f}億超")
                if data.get("per") and data["per"] > KIYOHARA_RULES["max_per"]:
                    reasons.append(f"PER{data['per']:.1f}倍超")
                if data.get("pbr") and data["pbr"] > KIYOHARA_RULES["max_pbr"]:
                    reasons.append(f"PBR{data['pbr']:.2f}倍超")
                if data.get("net_cash_ratio") is not None and data["net_cash_ratio"] < KIYOHARA_RULES["min_net_cash_ratio"]:
                    reasons.append(f"NC比率{data['net_cash_ratio']:.2f}未満")
                elif data.get("net_cash_ratio") is None:
                    reasons.append("NC比率不明")
                print(f"❌ 不合格 ({', '.join(reasons)})")

    if verbose:
        print(f"\n{'='*50}")
        print(f"📊 スクリーニング完了: {len(passed)}/{total}銘柄が条件通過")
        if passed:
            print(f"   上位5銘柄:")
            for p in sorted(passed, key=lambda x: x.get("score", 0), reverse=True)[:5]:
                print(f"   - {p['company_name']}({p['ticker']}) "
                      f"NC比率:{p.get('net_cash_ratio', 'N/A')} "
                      f"スコア:{p.get('score', 0):.1f}")
        print(f"{'='*50}\n")

    return sorted(passed, key=lambda x: x.get("score", 0), reverse=True)


def get_screened_candidates() -> List[Dict]:
    """DBからスクリーニング通過銘柄を取得（スコア降順）"""
    return get_screened_stocks()
