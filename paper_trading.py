"""
paper_trading.py
----------------
仮想運用(ペーパートレード)エンジン。実際のお金は一切動かさない。

「このルールで本当に買っていたら、今いくらになっていたか」を毎日計算して、
ルールの実力を確かめるためのもの。実際に個別株を買い始める前の"練習試合"。

■ 仕組み
    - 毎回 PAPER_START_DATE から最新データ日までを最初から計算し直す
      (状態ファイル不要。何度実行しても同じ結果になる)
    - 売買判断は「その日の終値までの情報」だけで行い、約定は翌営業日の始値(寄付)
      → SBI証券のS株など、単元未満株の寄付取引と同じ約定の仕方。先読みもしない
    - 損切り: 終値が損切りライン(買値-8%)を下回ったら、翌営業日の寄付で売却。
      単元未満株は逆指値(自動損切り)が使えないことが多いので、
      「毎朝レポートを見て手動で売る」という現実の運用に合わせている
    - 1株単位で買える前提(単元未満株)。手数料0円+スリッページ等を片道COST_PCT%見込む

■ 戦略(3つを並べて比べる)
    順張り:   毎週はじめに「上昇トレンド中の銘柄」をスコア順に上位3つ持つ。
              トップ3から外れたら売る
    逆張り:   RSIが30以下に売られた銘柄を買い、RSIが50に戻ったら売る
              (地合いが弱い日は買わない / 20営業日たっても戻らなければ売る)
    比較用:   10銘柄を最初に均等に買って、ずっと持ち続けるだけ(何もしない戦略)
    どの戦略も、損切りは買値から-8%。

■ 注意
    - ルールを途中で変えると、過去分も新しいルールで計算し直される(=ただの過去検証になる)。
      本当の実力が分かるのは RULES_FIXED_DATE 以降の「フォワード期間」の成績。
    - 仮想運用の成績が良くても、実際の売買で同じ結果になる保証はない。
"""

import math
from dataclasses import dataclass, field

import pandas as pd

PAPER_START_DATE = "2026-01-05"   # 仮想運用の開始日(データ日付)
RULES_FIXED_DATE = "2026-07-23"   # このルールを確定した時点の最新データ日の翌日。以降がフォワード期間
INITIAL_CAPITAL = 1_000_000       # 仮想の元手(円)
MAX_POSITIONS = 3                 # 同時に持つ最大銘柄数
STOP_LOSS_PCT = 8.0               # 損切りライン(買値から何%下落で売るか)
COST_PCT = 0.1                    # 片道のコスト見込み(%)
LOT_SIZE = 1                      # 1株単位(単元未満株)。単元株(100株)で試すなら100
STOP_COOLDOWN_DAYS = 10           # 損切りした銘柄は、この営業日数のあいだ買い直さない(往復ビンタ防止)

MR_ENTRY_RSI = 30                 # 逆張り: このRSI以下で買い
MR_EXIT_RSI = 50                  # 逆張り: このRSI以上に戻ったら売り
MR_MAX_HOLD_DAYS = 20             # 逆張り: 最大保有日数(営業日)
MR_MIN_BREADTH = 40               # 逆張り: 地合い(上昇トレンド銘柄の比率%)がこれ未満の日は買わない


@dataclass
class Position:
    code: str
    shares: int
    entry_price: float
    entry_date: pd.Timestamp
    stop_price: float
    hold_days: int = 0


@dataclass
class Order:
    code: str
    side: str      # "buy" or "sell"
    reason: str


@dataclass
class Account:
    name: str
    max_positions: int = MAX_POSITIONS
    use_stop: bool = True
    cash: float = INITIAL_CAPITAL
    positions: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    trades: list = field(default_factory=list)       # 決済済みの取引
    equity: list = field(default_factory=list)       # (日付, 評価額)
    fills_by_date: dict = field(default_factory=dict)  # 日付 -> 約定の説明リスト
    cooldown: dict = field(default_factory=dict)       # 銘柄 -> 買い直し禁止の残り営業日数

    def can_buy(self, code):
        return self.cooldown.get(code, 0) <= 0

    def log_fill(self, date, text):
        self.fills_by_date.setdefault(date, []).append(text)


def _row(panel, code, date):
    df = panel.get(code)
    if df is None or date not in df.index:
        return None
    return df.loc[date]


def _equity(account, panel, date, price_col="Close"):
    value = account.cash
    for p in account.positions.values():
        row = _row(panel, p.code, date)
        price = row[price_col] if row is not None else p.entry_price
        value += p.shares * price
    return value


def _sell(account, pos, price, date, reason, names):
    fill = price * (1 - COST_PCT / 100)
    account.cash += pos.shares * fill
    ret = (fill - pos.entry_price) / pos.entry_price * 100
    account.trades.append({"code": pos.code, "entry_date": pos.entry_date, "exit_date": date,
                           "entry_price": pos.entry_price, "exit_price": fill, "return_pct": ret, "reason": reason})
    del account.positions[pos.code]
    if reason.startswith("損切り"):
        account.cooldown[pos.code] = STOP_COOLDOWN_DAYS
    account.log_fill(date, f"{names.get(pos.code, pos.code)}を売却({reason}、{ret:+.1f}%)")


def _execute_pending(account, panel, date, names):
    """前日に決めた注文を、今日の始値で約定させる。売り→買いの順(売って空いた資金で買う)。"""
    carry = []
    for order in sorted(account.pending, key=lambda o: o.side != "sell"):
        row = _row(panel, order.code, date)
        if row is None:
            carry.append(order)  # その日データがない銘柄は翌日に持ち越し
            continue
        if order.side == "sell" and order.code in account.positions:
            _sell(account, account.positions[order.code], row["Open"], date, order.reason, names)
        elif (order.side == "buy" and order.code not in account.positions
              and len(account.positions) < account.max_positions):
            fill = row["Open"] * (1 + COST_PCT / 100)
            budget = min(account.cash, _equity(account, panel, date, "Open") / account.max_positions)
            shares = int(budget // (fill * LOT_SIZE)) * LOT_SIZE
            if shares <= 0:
                continue
            account.cash -= shares * fill
            account.positions[order.code] = Position(order.code, shares, fill, date,
                                                     fill * (1 - STOP_LOSS_PCT / 100))
            account.log_fill(date, f"{names.get(order.code, order.code)}を{shares}株購入({order.reason})")
    account.pending = carry


def _stop_orders(account, panel, date):
    """
    終値が損切りラインを割っていたら、翌営業日の寄付で売る注文を出す。
    単元未満株は逆指値(自動損切り)が使えないことが多いので、
    「毎朝レポートを見て手動で売る」という現実の運用に合わせている。
    """
    if not account.use_stop:
        return []
    orders = []
    for pos in account.positions.values():
        row = _row(panel, pos.code, date)
        if row is not None and row["Close"] <= pos.stop_price:
            orders.append(Order(pos.code, "sell", f"損切り-{STOP_LOSS_PCT:.0f}%"))
    return orders


# ============ 戦略 ============

def decide_momentum(account, panel, date, breadth, is_rebalance_day):
    """順張り: 週はじめに、上昇トレンド中の銘柄をスコア上位からMAX_POSITIONS個持つ。"""
    if not is_rebalance_day:
        return []
    candidates = []
    for code in panel:
        row = _row(panel, code, date)
        if row is not None and row["Close"] > row["MA25"] > row["MA75"] and (
                code in account.positions or account.can_buy(code)):
            candidates.append((row["score"], code))
    target = [code for _, code in sorted(candidates, reverse=True)[:account.max_positions]]
    orders = [Order(c, "sell", "週次入れ替え(上位圏外)") for c in account.positions if c not in target]
    free = account.max_positions - (len(account.positions) - len(orders))
    for c in target:
        if c not in account.positions and free > 0:
            orders.append(Order(c, "buy", "上昇トレンド・スコア上位"))
            free -= 1
    return orders


def decide_mean_reversion(account, panel, date, breadth, is_rebalance_day):
    """逆張り: RSI30以下で買い、RSI50回復 or 20営業日経過で売り。地合いが弱い日は買わない。"""
    orders = []
    for pos in account.positions.values():
        row = _row(panel, pos.code, date)
        if row is None:
            continue
        pos.hold_days += 1
        if row["RSI14"] >= MR_EXIT_RSI:
            orders.append(Order(pos.code, "sell", f"RSI{MR_EXIT_RSI}回復"))
        elif pos.hold_days >= MR_MAX_HOLD_DAYS:
            orders.append(Order(pos.code, "sell", f"{MR_MAX_HOLD_DAYS}日経過"))

    if breadth < MR_MIN_BREADTH:
        return orders
    free = account.max_positions - len(account.positions)
    candidates = []
    for code in panel:
        row = _row(panel, code, date)
        if (row is not None and code not in account.positions and account.can_buy(code)
                and row["RSI14"] <= MR_ENTRY_RSI):
            candidates.append((row["RSI14"], code))
    for _, code in sorted(candidates)[:max(free, 0)]:
        orders.append(Order(code, "buy", f"RSI{MR_ENTRY_RSI}以下"))
    return orders


def decide_buy_and_hold(account, panel, date, breadth, is_rebalance_day):
    """比較用: 初日に全銘柄を均等に買って、あとは何もしない。"""
    if account.positions or account.trades or account.equity[:-1]:
        return []
    return [Order(code, "buy", "初回の均等買い") for code in panel]


# ============ 実行 ============

def _summarize(account, panel, last_date, names):
    eq = pd.Series(dict(account.equity))
    total = (eq.iloc[-1] / INITIAL_CAPITAL - 1) * 100 if len(eq) else 0.0
    mdd = ((eq - eq.cummax()) / eq.cummax() * 100).min() if len(eq) else 0.0
    closed = account.trades
    wins = sum(1 for t in closed if t["return_pct"] > 0)
    holdings = []
    for p in account.positions.values():
        row = _row(panel, p.code, last_date)
        price = row["Close"] if row is not None else p.entry_price
        holdings.append({"name": names.get(p.code, p.code), "unrealized_pct": (price - p.entry_price) / p.entry_price * 100})
    return {
        "name": account.name,
        "equity": float(eq.iloc[-1]) if len(eq) else INITIAL_CAPITAL,
        "total_return_pct": float(total),
        "max_drawdown_pct": float(mdd),
        "n_trades": len(closed),
        "win_rate_pct": (wins / len(closed) * 100) if closed else None,
        "holdings": holdings,
        "today_fills": account.fills_by_date.get(last_date, []),
        "next_orders": [f"{names.get(o.code, o.code)}を{'購入' if o.side == 'buy' else '売却'}予定({o.reason})"
                        for o in account.pending],
    }


def run_paper_trading(dfs: dict, names: dict) -> dict | None:
    """
    dfs: 銘柄コード -> 指標計算済みDataFrame(Date, Open, Low, Close, MA25, MA75, RSI14, score を含む)
    戻り値: 戦略ごとの成績の要約
    """
    start = pd.Timestamp(PAPER_START_DATE)
    panel = {}
    for code, df in dfs.items():
        d = df.set_index("Date")
        d = d[d.index >= start].dropna(subset=["MA75", "RSI14", "score"])
        if not d.empty:
            panel[code] = d
    if not panel:
        return None

    dates = sorted(set().union(*[set(d.index) for d in panel.values()]))
    breadth_by_date = {}
    for date in dates:
        rows = [r for r in (_row(panel, c, date) for c in panel) if r is not None]
        breadth_by_date[date] = (sum(r["MA25"] > r["MA75"] for r in rows) / len(rows) * 100) if rows else 0

    strategies = [
        (Account("順張り(強い株を持つ)"), decide_momentum),
        (Account("逆張り(売られすぎを拾う)"), decide_mean_reversion),
        (Account("比較: 均等に買って放置", max_positions=len(panel), use_stop=False), decide_buy_and_hold),
    ]
    prev_week = None
    for date in dates:
        week = date.isocalendar()[:2]
        is_rebalance_day = week != prev_week
        prev_week = week
        for account, decide in strategies:
            account.cooldown = {c: n - 1 for c, n in account.cooldown.items() if n > 1}
            _execute_pending(account, panel, date, names)
            account.equity.append((date, _equity(account, panel, date)))
            new_orders = _stop_orders(account, panel, date)
            new_orders += decide(account, panel, date, breadth_by_date[date], is_rebalance_day)
            seen = {(o.code, o.side) for o in account.pending}
            for o in new_orders:
                if (o.code, o.side) not in seen:
                    account.pending.append(o)
                    seen.add((o.code, o.side))

    last_date = dates[-1]
    forward_days = sum(1 for d in dates if d >= pd.Timestamp(RULES_FIXED_DATE))
    return {
        "start": dates[0].date(),
        "last_date": last_date.date(),
        "forward_days": forward_days,
        "strategies": [_summarize(a, panel, last_date, names) for a, _ in strategies],
    }
