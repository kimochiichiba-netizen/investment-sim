"""
財務データ取得モジュール（清原式ファンダメンタル分析用）
yfinance を使って財務指標を取得し、1日1回だけAPIを叩くキャッシュ戦略を採用します。

【取得する指標】
- 時価総額（億円）
- PER（株価収益率）: 株価 ÷ 1株利益。低いほど割安
- PBR（株価純資産倍率）: 株価 ÷ 1株純資産。低いほど割安
- 流動資産（億円）: 1年以内に現金化できる資産
- 負債合計（億円）: 会社が返さないといけないお金
- ネットキャッシュ（億円）: 流動資産 - 負債合計
- ネットキャッシュ比率: ネットキャッシュ ÷ 時価総額
  ※ 1.0以上なら「会社の現金が時価総額より多い」= 超割安
"""
import yfinance as yf
from typing import Optional, Dict
from datetime import date

from database import get_fundamental_cache, save_fundamental_cache


def _to_oku(value) -> Optional[float]:
    """円 → 億円に変換。Noneの場合はNoneを返す"""
    if value is None:
        return None
    try:
        return round(float(value) / 1e8, 2)
    except (TypeError, ValueError):
        return None


def _get_balance_sheet_value(bs, *keys) -> Optional[float]:
    """
    バランスシートから値を取得する。
    yfinanceはバージョンによってキー名が変わるため、複数候補を試す。

    bs: yfinance の balance_sheet (DataFrame)
    keys: 試すキーのリスト
    """
    if bs is None or bs.empty:
        return None
    for key in keys:
        if key in bs.index:
            try:
                # 最新年度（列の先頭）を使う
                val = bs.loc[key].iloc[0]
                if val is not None and str(val) not in ("nan", "None"):
                    return float(val)
            except Exception:
                continue
    return None


def get_fundamental(ticker: str) -> Optional[Dict]:
    """
    指定銘柄の財務データを取得する。
    今日のキャッシュが DB にあれば API を叩かずに返す（高速＆節約）。

    返り値の例:
    {
        "ticker": "3632.T",
        "company_name": "グリー",
        "market_cap_oku": 120.5,    # 時価総額（億円）
        "per": 6.2,                  # PER
        "pbr": 0.8,                  # PBR
        "current_assets_oku": 350.0, # 流動資産（億円）
        "total_liabilities_oku": 80.0, # 負債合計（億円）
        "net_cash_oku": 270.0,       # ネットキャッシュ（億円）
        "net_cash_ratio": 2.24,      # ネットキャッシュ比率
        "dividend_yield": 3.5,       # 配当利回り（%）
        "sector": "Technology",
        "last_updated": "2025-01-15",
    }
    """
    # ① 今日のキャッシュが DB にあればそのまま返す
    cached = get_fundamental_cache(ticker)
    if cached:
        return cached

    # ② キャッシュがなければ yfinance で取得
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info or {}

        # 時価総額
        market_cap_raw = info.get("marketCap")
        market_cap_oku = _to_oku(market_cap_raw)

        # PER / PBR
        per = info.get("trailingPE") or info.get("forwardPE")
        pbr = info.get("priceToBook")
        if per is not None:
            per = round(float(per), 2)
        if pbr is not None:
            pbr = round(float(pbr), 2)

        # 配当利回り（% 換算）
        div_yield_raw = info.get("dividendYield")
        dividend_yield = round(float(div_yield_raw) * 100, 2) if div_yield_raw else None

        # セクター
        sector = info.get("sector") or info.get("industry")

        # 会社名（日本語が取れることもある）
        company_name = info.get("longName") or info.get("shortName") or ticker

        # バランスシートから流動資産・負債を取得
        try:
            bs = stock.balance_sheet
        except Exception:
            bs = None

        current_assets_raw = _get_balance_sheet_value(
            bs,
            "Current Assets",
            "currentAssets",
            "Total Current Assets",
            "TotalCurrentAssets",
        )
        total_liabilities_raw = _get_balance_sheet_value(
            bs,
            "Total Liabilities Net Minority Interest",
            "totalLiabilitiesNetMinorityInterest",
            "Total Liabilities",
            "totalLiabilities",
            "Liabilities",
        )

        current_assets_oku   = _to_oku(current_assets_raw)
        total_liabilities_oku = _to_oku(total_liabilities_raw)

        # ネットキャッシュ計算
        if current_assets_oku is not None and total_liabilities_oku is not None:
            net_cash_oku = round(current_assets_oku - total_liabilities_oku, 2)
        else:
            net_cash_oku = None

        # ネットキャッシュ比率 = ネットキャッシュ ÷ 時価総額
        if net_cash_oku is not None and market_cap_oku and market_cap_oku > 0:
            net_cash_ratio = round(net_cash_oku / market_cap_oku, 3)
        else:
            net_cash_ratio = None

        data = {
            "ticker":               ticker,
            "company_name":         company_name,
            "market_cap_oku":       market_cap_oku,
            "per":                  per,
            "pbr":                  pbr,
            "current_assets_oku":   current_assets_oku,
            "total_liabilities_oku": total_liabilities_oku,
            "net_cash_oku":         net_cash_oku,
            "net_cash_ratio":       net_cash_ratio,
            "dividend_yield":       dividend_yield,
            "sector":               sector,
            "last_updated":         date.today().isoformat(),
        }

        # DB にキャッシュ保存（以降はAPIを叩かない）
        try:
            save_fundamental_cache(data)
        except Exception as e:
            print(f"⚠️  {ticker} キャッシュ保存エラー: {e}")

        return data

    except Exception as e:
        print(f"⚠️  {ticker} 財務データ取得エラー: {e}")
        return None


def get_current_price(ticker: str) -> Optional[float]:
    """指定銘柄の現在株価を取得する"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 1)
    except Exception as e:
        print(f"⚠️  {ticker} 株価取得エラー: {e}")
        return None
