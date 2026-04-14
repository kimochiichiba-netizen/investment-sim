"""
データベース処理
保有株・取引履歴・資金残高を SQLite に保存します
"""
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

DB_PATH = "investment.db"
INITIAL_CAPITAL = 1_000_000  # 初期資金 100万円


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # dict風に取得できるようにする
    return conn


def init_db():
    """テーブルを初期化する（初回起動時のみ実行）"""
    conn = get_conn()
    cur = conn.cursor()

    # 資金テーブル（1行のみ）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            initial_capital REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 保有株テーブル
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

    # 取引履歴テーブル
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

    # 資産推移テーブル（グラフ用）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS asset_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_assets REAL NOT NULL,
            cash REAL NOT NULL,
            stock_value REAL NOT NULL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    """現在の資金情報を取得"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    conn.close()
    return dict(row)


def update_cash(new_cash: float):
    """現金残高を更新"""
    conn = get_conn()
    conn.execute(
        "UPDATE account SET cash = ?, updated_at = ? WHERE id = 1",
        (new_cash, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ==================== ポートフォリオ操作 ====================

def get_portfolio() -> List[Dict]:
    """保有株を全件取得"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM portfolio ORDER BY ticker").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_holding(ticker: str) -> Optional[Dict]:
    """指定銘柄の保有情報を取得"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM portfolio WHERE ticker = ?", (ticker,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_holding(ticker: str, company_name: str, shares: int, avg_cost: float):
    """保有株を追加・更新"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO portfolio (ticker, company_name, shares, avg_cost)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            shares = ?,
            avg_cost = ?,
            company_name = ?
    """, (ticker, company_name, shares, avg_cost, shares, avg_cost, company_name))
    conn.commit()
    conn.close()


def delete_holding(ticker: str):
    """保有株を削除（全売却時）"""
    conn = get_conn()
    conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()


# ==================== 取引履歴 ====================

def save_trade(
    ticker: str,
    company_name: str,
    action: str,
    shares: int,
    price: float,
    total_amount: float,
    commission: float,
    reason: str,
):
    """取引を記録する"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades
            (ticker, company_name, action, shares, price, total_amount, commission, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, company_name, action, shares, price, total_amount, commission, reason))
    conn.commit()
    conn.close()


def get_trades(limit: int = 50) -> List[Dict]:
    """取引履歴を最新順で取得"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ==================== 資産推移 ====================

def save_asset_snapshot(total_assets: float, cash: float, stock_value: float):
    """現在の資産状況をスナップショット保存（グラフ用）"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO asset_history (total_assets, cash, stock_value)
        VALUES (?, ?, ?)
    """, (total_assets, cash, stock_value))
    conn.commit()
    conn.close()


def get_asset_history(days: int = 30) -> List[Dict]:
    """資産推移を取得（1日1件）"""
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


# ==================== リセット ====================

def reset_all():
    """全データをリセットして初期状態に戻す"""
    conn = get_conn()
    conn.execute("DELETE FROM portfolio")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM asset_history")
    conn.execute(
        "UPDATE account SET cash = ?, updated_at = ? WHERE id = 1",
        (INITIAL_CAPITAL, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    print("🔄 データをリセットしました")
