"""
データベース処理
保有株・取引履歴・資金残高・財務データを SQLite に保存します
"""
import sqlite3
from datetime import datetime, date
from typing import List, Dict, Optional

DB_PATH = "investment.db"
INITIAL_CAPITAL = 2_000_000  # 初期資金 200万円


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_not_exists(cur, table: str, column: str, col_type: str):
    """列が存在しなければ追加する（2回目以降のinit_dbでエラーにならないように）"""
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # すでに存在する


def init_db():
    """テーブルを初期化する"""
    conn = get_conn()
    cur = conn.cursor()

    # ── 既存テーブル ──────────────────────────────────────────

    cur.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            initial_capital REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            company_name TEXT NOT NULL,
            shares INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # portfolioテーブルに列を追加（既存DBとの互換性）
    for col, typ in [
        ("buy_per",             "REAL"),
        ("buy_pbr",             "REAL"),
        ("buy_net_cash_ratio",  "REAL"),
        ("target_price",        "REAL"),
        ("catalyst_notes",      "TEXT"),
        # トレーリングストップ管理（プロ仕様リスク管理）
        ("peak_price",          "REAL"),   # 購入後の最高値
        ("trailing_stop",       "REAL"),   # 現在のストップ価格
        ("partial_taken",       "INTEGER DEFAULT 0"),  # 部分利確済みフラグ
    ]:
        _add_column_if_not_exists(cur, "portfolio", col, typ)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            company_name TEXT NOT NULL,
            action TEXT NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            total_amount REAL NOT NULL,
            commission REAL NOT NULL,
            reason TEXT,
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS asset_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_assets REAL NOT NULL,
            cash REAL NOT NULL,
            stock_value REAL NOT NULL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── 新テーブル ────────────────────────────────────────────

    # 財務データキャッシュ（1日1回だけAPIを叩く）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fundamental_cache (
            ticker TEXT PRIMARY KEY,
            company_name TEXT,
            market_cap_oku REAL,
            per REAL,
            pbr REAL,
            current_assets_oku REAL,
            total_liabilities_oku REAL,
            net_cash_oku REAL,
            net_cash_ratio REAL,
            dividend_yield REAL,
            sector TEXT,
            last_updated DATE
        )
    """)

    # スクリーニング通過銘柄（投資候補）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS screened_stocks (
            ticker TEXT PRIMARY KEY,
            company_name TEXT,
            market_cap_oku REAL,
            per REAL,
            pbr REAL,
            current_assets_oku REAL,
            total_liabilities_oku REAL,
            net_cash_oku REAL,
            net_cash_ratio REAL,
            score REAL,
            screened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # アカウントが存在しなければ初期化
    cur.execute("SELECT id FROM account WHERE id = 1")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO account (id, cash, initial_capital) VALUES (1, ?, ?)",
            (INITIAL_CAPITAL, INITIAL_CAPITAL)
        )
        print(f"✅ 初期資金 {INITIAL_CAPITAL:,}円 でアカウントを作成しました")

    conn.commit()
    conn.close()
    print("✅ データベースの初期化が完了しました")


# ==================== 資金操作 ====================

def get_account() -> Dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    conn.close()
    return dict(row)


def update_cash(new_cash: float):
    conn = get_conn()
    conn.execute(
        "UPDATE account SET cash = ?, updated_at = ? WHERE id = 1",
        (new_cash, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ==================== ポートフォリオ操作 ====================

def get_portfolio() -> List[Dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM portfolio ORDER BY ticker").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_holding(ticker: str) -> Optional[Dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM portfolio WHERE ticker = ?", (ticker,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_holding(
    ticker: str,
    company_name: str,
    shares: int,
    avg_cost: float,
    buy_per: Optional[float] = None,
    buy_pbr: Optional[float] = None,
    buy_net_cash_ratio: Optional[float] = None,
    target_price: Optional[float] = None,
    catalyst_notes: Optional[str] = None,
    peak_price: Optional[float] = None,
    trailing_stop: Optional[float] = None,
):
    """保有株を追加・更新（ファンダメンタル＋トレーリングストップ情報も保存）"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO portfolio
            (ticker, company_name, shares, avg_cost,
             buy_per, buy_pbr, buy_net_cash_ratio, target_price, catalyst_notes,
             peak_price, trailing_stop)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            shares = ?,
            avg_cost = ?,
            company_name = ?
    """, (
        ticker, company_name, shares, avg_cost,
        buy_per, buy_pbr, buy_net_cash_ratio, target_price, catalyst_notes,
        peak_price, trailing_stop,
        shares, avg_cost, company_name,
    ))
    conn.commit()
    conn.close()


def update_trailing_stop(ticker: str, peak_price: float, trailing_stop: float):
    """トレーリングストップの価格を更新する（毎分実行）"""
    conn = get_conn()
    conn.execute(
        "UPDATE portfolio SET peak_price = ?, trailing_stop = ? WHERE ticker = ?",
        (peak_price, trailing_stop, ticker)
    )
    conn.commit()
    conn.close()


def mark_partial_taken(ticker: str):
    """部分利確済みフラグを立てる"""
    conn = get_conn()
    conn.execute("UPDATE portfolio SET partial_taken = 1 WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


def get_trade_stats() -> Dict:
    """取引統計（勝率・平均損益）を計算して返す"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT action, ticker, shares, price, total_amount, commission
        FROM trades ORDER BY executed_at
    """).fetchall()
    conn.close()

    buy_map = {}   # ticker → [(shares, price), ...]
    wins, losses = 0, 0
    total_pnl = 0.0

    for r in rows:
        t = r["ticker"]
        if r["action"] == "buy":
            if t not in buy_map:
                buy_map[t] = []
            buy_map[t].append((r["shares"], r["price"]))
        elif r["action"] == "sell":
            if t in buy_map and buy_map[t]:
                avg_buy = sum(s * p for s, p in buy_map[t]) / sum(s for s, _ in buy_map[t])
                pnl = (r["price"] - avg_buy) * r["shares"] - r["commission"]
                total_pnl += pnl
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    return {
        "total_trades": total_trades,
        "wins":        wins,
        "losses":      losses,
        "win_rate":    round(win_rate, 1),
        "total_pnl":   round(total_pnl, 0),
    }


def delete_holding(ticker: str):
    conn = get_conn()
    conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


def recently_sold(ticker: str, days: int = 7) -> bool:
    """
    指定銘柄を直近 days 日以内に売却していれば True を返す。
    クールダウン期間のチェックに使い、「売ってすぐ再購入→手数料二重払い」を防ぐ。
    """
    conn = get_conn()
    row = conn.execute("""
        SELECT id FROM trades
        WHERE ticker = ?
          AND action = 'sell'
          AND executed_at >= datetime('now', ?)
        LIMIT 1
    """, (ticker, f'-{days} days')).fetchone()
    conn.close()
    return row is not None


# ==================== 取引履歴 ====================

def save_trade(
    ticker: str, company_name: str, action: str,
    shares: int, price: float, total_amount: float,
    commission: float, reason: str,
):
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades
            (ticker, company_name, action, shares, price, total_amount, commission, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, company_name, action, shares, price, total_amount, commission, reason))
    conn.commit()
    conn.close()


def get_trades(limit: int = 50) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ==================== 資産推移 ====================

def save_asset_snapshot(total_assets: float, cash: float, stock_value: float):
    conn = get_conn()
    conn.execute(
        "INSERT INTO asset_history (total_assets, cash, stock_value) VALUES (?, ?, ?)",
        (total_assets, cash, stock_value)
    )
    conn.commit()
    conn.close()


def get_asset_history(days: int = 30) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            DATE(recorded_at) as date,
            AVG(total_assets) as total_assets,
            AVG(cash) as cash,
            AVG(stock_value) as stock_value
        FROM asset_history
        WHERE recorded_at >= datetime('now', ?)
        GROUP BY DATE(recorded_at)
        ORDER BY date
    """, (f'-{days} days',)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ==================== 財務データキャッシュ ====================

def get_fundamental_cache(ticker: str) -> Optional[Dict]:
    """今日のキャッシュがあれば返す（なければNone）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM fundamental_cache WHERE ticker = ? AND last_updated = ?",
        (ticker, date.today().isoformat())
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_fundamental_cache(data: Dict):
    """財務データをキャッシュに保存（upsert）"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO fundamental_cache
            (ticker, company_name, market_cap_oku, per, pbr,
             current_assets_oku, total_liabilities_oku, net_cash_oku,
             net_cash_ratio, dividend_yield, sector, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            company_name = ?, market_cap_oku = ?, per = ?, pbr = ?,
            current_assets_oku = ?, total_liabilities_oku = ?,
            net_cash_oku = ?, net_cash_ratio = ?,
            dividend_yield = ?, sector = ?, last_updated = ?
    """, (
        data["ticker"], data["company_name"], data["market_cap_oku"],
        data["per"], data["pbr"], data["current_assets_oku"],
        data["total_liabilities_oku"], data["net_cash_oku"],
        data["net_cash_ratio"], data.get("dividend_yield"),
        data.get("sector"), data["last_updated"],
        # UPDATE SET の値
        data["company_name"], data["market_cap_oku"], data["per"], data["pbr"],
        data["current_assets_oku"], data["total_liabilities_oku"],
        data["net_cash_oku"], data["net_cash_ratio"],
        data.get("dividend_yield"), data.get("sector"), data["last_updated"],
    ))
    conn.commit()
    conn.close()


# ==================== スクリーニング ====================

def save_screened_stock(data: Dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO screened_stocks
            (ticker, company_name, market_cap_oku, per, pbr,
             current_assets_oku, total_liabilities_oku, net_cash_oku,
             net_cash_ratio, score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            company_name = ?, market_cap_oku = ?, per = ?, pbr = ?,
            current_assets_oku = ?, total_liabilities_oku = ?,
            net_cash_oku = ?, net_cash_ratio = ?, score = ?,
            screened_at = CURRENT_TIMESTAMP
    """, (
        data["ticker"], data["company_name"], data["market_cap_oku"],
        data["per"], data["pbr"], data["current_assets_oku"],
        data["total_liabilities_oku"], data["net_cash_oku"],
        data["net_cash_ratio"], data["score"],
        data["company_name"], data["market_cap_oku"], data["per"], data["pbr"],
        data["current_assets_oku"], data["total_liabilities_oku"],
        data["net_cash_oku"], data["net_cash_ratio"], data["score"],
    ))
    conn.commit()
    conn.close()


def get_screened_stocks() -> List[Dict]:
    """スクリーニング通過銘柄をスコア降順で取得"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM screened_stocks ORDER BY score DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_screened_stocks():
    """スクリーニング結果をクリア（再スクリーニング前に実行）"""
    conn = get_conn()
    conn.execute("DELETE FROM screened_stocks")
    conn.commit()
    conn.close()


def is_screened(ticker: str) -> bool:
    """指定銘柄がスクリーニング通過中か確認"""
    conn = get_conn()
    row = conn.execute(
        "SELECT ticker FROM screened_stocks WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    return row is not None


# ==================== 確定損益（買い→売りペア） ====================

def get_closed_trades(limit: int = 30) -> List[Dict]:
    """
    完了済み取引（買い→売りのペア）を返す
    直近の sell をベースに、同銘柄の直近 buy と突き合わせて損益を計算する
    """
    conn = get_conn()
    # 同じ銘柄・日時・価格の重複レコードを除外（MIN(id)を使って最初の1件だけ取得）
    sells = conn.execute("""
        SELECT * FROM trades
        WHERE id IN (
            SELECT MIN(id) FROM trades
            WHERE action='sell'
            GROUP BY ticker, executed_at, price
        )
        ORDER BY executed_at DESC LIMIT ?
    """, (limit,)).fetchall()

    result = []
    for sell in sells:
        sell = dict(sell)
        buy = conn.execute("""
            SELECT * FROM trades WHERE ticker=? AND action='buy'
            AND executed_at <= ?
            ORDER BY executed_at DESC LIMIT 1
        """, (sell["ticker"], sell["executed_at"])).fetchone()
        buy = dict(buy) if buy else None

        buy_price = buy["price"] if buy else sell["price"]
        shares = sell["shares"]
        sell_price = sell["price"]
        commission = sell["commission"]
        pnl = (sell_price - buy_price) * shares - commission
        pnl_pct = ((sell_price / buy_price) - 1) * 100 if buy_price else 0

        result.append({
            "ticker":       sell["ticker"],
            "company_name": sell["company_name"],
            "buy_price":    round(buy_price, 1),
            "sell_price":   round(sell_price, 1),
            "shares":       shares,
            "pnl":          round(pnl, 0),
            "pnl_pct":      round(pnl_pct, 2),
            "reason":       sell["reason"],
            "sell_date":    sell["executed_at"],
            "buy_date":     buy["executed_at"] if buy else None,
        })

    conn.close()
    return result


# ==================== リセット ====================

def reset_all():
    """全データをリセットして初期状態（200万円）に戻す"""
    conn = get_conn()
    conn.execute("DELETE FROM portfolio")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM asset_history")
    conn.execute("DELETE FROM screened_stocks")
    conn.execute(
        "UPDATE account SET cash = ?, initial_capital = ?, updated_at = ? WHERE id = 1",
        (INITIAL_CAPITAL, INITIAL_CAPITAL, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    print("🔄 データをリセットしました（初期資金200万円）")
