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

    # portfolioテーブルにファンダメンタル列を追加（既存DBとの互換性）
    for col, typ in [
        ("buy_per",             "REAL"),
        ("buy_pbr",             "REAL"),
        ("buy_net_cash_ratio",  "REAL"),
        ("target_price",        "REAL"),
        ("catalyst_notes",      "TEXT"),
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
):
    """保有株を追加・更新（ファンダメンタル情報も一緒に保存）"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO portfolio
            (ticker, company_name, shares, avg_cost,
             buy_per, buy_pbr, buy_net_cash_ratio, target_price, catalyst_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            shares = ?,
            avg_cost = ?,
            company_name = ?
    """, (
        ticker, company_name, shares, avg_cost,
        buy_per, buy_pbr, buy_net_cash_ratio, target_price, catalyst_notes,
        shares, avg_cost, company_name,
    ))
    conn.commit()
    conn.close()


def delete_holding(ticker: str):
    conn = get_conn()
    conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


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
