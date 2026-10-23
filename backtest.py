# -*- coding: utf-8 -*-
"""
backtest.py — 「買い時ルール」を過去の値動きで試す（検証専用）
======================================================================
本番botと同じ判定関数（bot.evaluate）・同じ通知ルール（bot.decide_alert）を使い、
過去の価格データに当てはめて「買い時通知」を再現します。そのうえで、
  ・通知はどれくらいの頻度で来たか
  ・通知が出た時に買っていたら、その後どうなったか（普通に買った場合と比べて）
  ・通知のあとにさらに下がった幅（外れパターン）
  ・同じ金額を使うなら、押し目買いは定期買いより安く買えたか
を集計します。

■ このスクリプトは「読み取り専用」です。
  ・LINEは送りません（秘密情報・Secretsも使いません）
  ・注文も出しません。お金は一切動かしません
  ・価格データは bitbank の公開API（認証不要）から取得します
  ・過去の結果は将来を保証しません
"""

import argparse
import bisect
import datetime as dt
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict, deque

import requests

import bot  # 判定ロジック(evaluate)と通知ルール(decide_alert)は本番と同じものを使う

# ============================================================
# 設定
# ============================================================
PUBLIC_URL = "https://public.bitbank.cc"
PAIR = "btc_jpy"
CANDLE = "15min"
CANDLE_SECONDS = 15 * 60
CANDLES_PER_DAY = 96
FETCH_SLEEP_SECONDS = 0.2          # 公開APIへの連続アクセスを控えめにする（約5回/秒）
MAX_FAILED_DAY_RATIO = 0.05        # 取得に失敗した日がこれを超えたら、検証をやめる
MAX_CONSECUTIVE_FAILED_DAYS = 10   # 取得失敗が連続したら、待たずにすぐやめる（アクセス拒否などの早期発見）

JST = dt.timezone(dt.timedelta(hours=9))
HORIZONS_DAYS = (1, 3, 7, 14, 30)  # 通知(や判定)の何日後の値動きを見るか
HOLE_TOLERANCE_SECONDS = 6 * 3600  # 将来の価格がこれ以上ずれて見つかる時は、データ欠損として使わない

# 「実際に近い見られ方」: GitHub Actionsの自動実行が遅れて、1〜5.5時間おきにしか見られない場合
SPARSE_MIN_GAP_H = 0.5
SPARSE_MAX_GAP_H = 5.5
SPARSE_SEEDS = (1, 2, 3, 4, 5)

RANDOM_DRAWS = 2000                # 「ランダムに同じ回数買った場合」の試行回数
RANDOM_SEED = 12345
MIN_ALERTS_FOR_COMPARISON = 5

# 「偽の相場」（本物の値動きを1日ぶんずつ切って、順番だけシャッフルしたもの）で、偶然との見分けをつける
NULL_PATHS = 30                    # 偽の相場を何本作るか
NULL_BLOCK_CANDLES = CANDLES_PER_DAY
NULL_SEED = 777
NULL_RANDOM_DRAWS = 200            # 偽の相場では「ランダム買い」の試行を軽くする（等間隔との比較だけ使う）

ZONE_LABEL = {"buy": "買い時ゾーン", "watch": "静観ゾーン", "danger": "危険ゾーン"}


class BacktestError(Exception):
    pass


# ============================================================
# 価格データの取得（bitbank 公開API・認証不要）
# ============================================================
def fetch_candles_for_day(session, day, retries=3, info=None):
    """1日ぶん(UTC日付)の15分足を取得。戻り値: [(開始ms, 終値)] / 取得失敗は None。
    失敗した時は、原因（HTTPの番号など）を info["reason"] に入れる。"""
    url = f"{PUBLIC_URL}/{PAIR}/candlestick/{CANDLE}/{day:%Y%m%d}"
    reason = "unknown"
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20)
        except requests.exceptions.RequestException as e:
            reason = f"request_{type(e).__name__}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:     # 混雑・一時的な不調は、待ってやり直す
            reason = f"http_{resp.status_code}"
            time.sleep(2.0 * (attempt + 1))
            continue
        if resp.status_code != 200:
            reason = f"http_{resp.status_code}"
            break
        try:
            data = resp.json()
            if data.get("success") != 1:
                reason = "api_success_not_1"
                break
            ohlcv = data["data"]["candlestick"][0]["ohlcv"]
            return [(int(row[5]), float(row[3])) for row in ohlcv]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            reason = "unexpected_response_format"
            break
    if info is not None:
        info["reason"] = reason
    return None


def fetch_series(start_day, end_day, session=None, sleep=FETCH_SLEEP_SECONDS, log=print):
    """start_day〜end_day(UTC日付)の15分足を集めて、(時刻, 価格)の一覧を返す。
    時刻は「その足が終わった瞬間」（＝その価格が確定した時刻）にそろえる。
    戻り値: (series, meta)"""
    session = session or requests.Session()
    rows = {}
    failed, empty, days = [], 0, 0
    consecutive_failed, logged_failures = 0, 0
    day = start_day
    while day <= end_day:
        info = {}
        got = fetch_candles_for_day(session, day, info=info)
        days += 1
        if got is None:
            failed.append(day)
            consecutive_failed += 1
            if logged_failures < 5:
                log(f"[WARN] fetch_failed day={day:%Y%m%d} reason={info.get('reason')}")
                logged_failures += 1
            if consecutive_failed >= MAX_CONSECUTIVE_FAILED_DAYS:
                raise BacktestError(
                    f"価格データの取得に{consecutive_failed}日連続で失敗したので中止します（原因: {info.get('reason')}）。")
        else:
            consecutive_failed = 0
            if not got:
                empty += 1
            for ts_ms, close in got:
                if close > 0:
                    rows[ts_ms] = close
        if days % 200 == 0:
            log(f"[INFO] fetched days={days} candles={len(rows)} failed_days={len(failed)}")
        day += dt.timedelta(days=1)
        if sleep:
            time.sleep(sleep)

    if days == 0 or not rows:
        raise BacktestError("価格データを1件も取得できませんでした。")
    if len(failed) > days * MAX_FAILED_DAY_RATIO:
        raise BacktestError(f"価格データの取得に失敗した日が多すぎます（{len(failed)}日／{days}日）。")

    series = [(ts_ms / 1000.0 + CANDLE_SECONDS, rows[ts_ms]) for ts_ms in sorted(rows)]
    meta = {
        "start": dt.datetime.fromtimestamp(series[0][0], JST).strftime("%Y-%m-%d"),
        "end": dt.datetime.fromtimestamp(series[-1][0], JST).strftime("%Y-%m-%d"),
        "rows": len(series),
        "requested_days": days,
        "failed_days": len(failed),
        "empty_days": empty,
        "coverage_pct": len(rows) / (days * CANDLES_PER_DAY) * 100.0,
    }
    return series, meta


# ============================================================
# 本番と同じルールで「買い時通知」を再現する
# ============================================================
def simulate(series, mode_key=None):
    """bot.main() と同じ手順（履歴に追記→24時間で切る→判定→通知可否→状態更新）を、
    過去データの上で1ステップずつ再現する。"""
    mode_key = mode_key or bot.DEFAULT_MODE
    lookback = bot.LOOKBACK_HOURS * 3600
    store = {"last_zone": None, "last_notified_at": 0.0, "last_alert_price": None,
             "alert_day": None, "alerts_today": 0}
    hist = deque()
    times, prices, zones, devs, alerts = [], [], [], [], []

    for t, p in series:
        hist.append((t, p))
        cutoff = t - lookback
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        result = bot.evaluate(p, t, list(hist), mode_key)
        zone = result["zone"]
        kind = None
        if zone == "buy":
            send, k, why = bot.decide_alert(store, p, t)
            if send:
                nth = bot.alerts_sent_today(store, t) + 1
                store["last_notified_at"] = t
                store["last_alert_price"] = p
                store["alert_day"] = bot.jst_date_str(t)
                store["alerts_today"] = nth
                kind = k
            elif why == "no_baseline":
                store["last_alert_price"] = p
        store["last_zone"] = zone

        times.append(t)
        prices.append(p)
        zones.append(zone)
        devs.append(result["deviation_pct"])
        alerts.append(kind)

    return {"times": times, "prices": prices, "zones": zones, "devs": devs, "alerts": alerts}


def sparse_series(series, seed):
    """GitHub Actionsの自動実行が遅れて、1〜5.5時間おきにしか価格を見られない場合を真似て、
    15分足から飛び飛びに拾う（seedを固定して、毎回同じ結果になるようにする）。"""
    rng = random.Random(seed)
    times = [t for t, _ in series]
    out = []
    i, n = 0, len(series)
    while i < n:
        out.append(series[i])
        gap = rng.uniform(SPARSE_MIN_GAP_H, SPARSE_MAX_GAP_H) * 3600
        nxt = bisect.bisect_left(times, series[i][0] + gap)
        i = nxt if nxt > i else i + 1
    return out


# ============================================================
# 集計
# ============================================================
def forward_returns(times, prices, horizon_sec, tolerance=HOLE_TOLERANCE_SECONDS):
    """各時点から horizon_sec 後の騰落率（価格がその期間でどれだけ変わったか）。
    将来のデータが無い／欠損でずれすぎる時は None。"""
    n = len(times)
    out = [None] * n
    j = 0
    for i in range(n):
        target = times[i] + horizon_sec
        if j < i:
            j = i
        while j < n and times[j] < target:
            j += 1
        if j >= n:
            break
        if times[j] - target <= tolerance:
            out[i] = prices[j] / prices[i] - 1.0
    return out


def window_extremes(times, prices, i, window_sec):
    """i時点のあと window_sec の間に、価格が一番下がった／一番上がった時の騰落率。
    期間ぶんのデータが揃っていなければ None。"""
    end_t = times[i] + window_sec
    if times[-1] < end_t:
        return None
    j = bisect.bisect_right(times, end_t)
    seg = prices[i + 1:j]
    if not seg:
        return None
    base = prices[i]
    return min(seg) / base - 1.0, max(seg) / base - 1.0


def summarize(values):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    n = len(xs)
    return {
        "n": n,
        "mean": sum(xs) / n,
        "median": statistics.median(xs),
        "win": sum(1 for x in xs if x > 0) / n,
        "p10": xs[int(0.1 * (n - 1))],
        "worst": xs[0],
    }


def harmonic_cost(prices):
    """同じ金額ずつ買った時の平均購入価格（円/BTC）。"""
    return len(prices) / sum(1.0 / p for p in prices)


def alert_frequency(times, alerts):
    idx = [i for i, a in enumerate(alerts) if a]
    per_day, per_month = defaultdict(int), defaultdict(int)
    for i in idx:
        d = dt.datetime.fromtimestamp(times[i], JST)
        per_day[d.date()] += 1
        per_month[(d.year, d.month)] += 1
    first_d = dt.datetime.fromtimestamp(times[0], JST).date()
    last_d = dt.datetime.fromtimestamp(times[-1], JST).date()
    days_total = (last_d - first_d).days + 1
    months_span = days_total / 30.44
    return {
        "total": len(idx),
        "first": sum(1 for a in alerts if a == "first"),
        "followup": sum(1 for a in alerts if a == "followup"),
        "per_month_avg": (len(idx) / months_span) if months_span > 0 else None,
        "per_month_max": max(per_month.values()) if per_month else 0,
        "days_total": days_total,
        "days_with_alert": len(per_day),
        "days_with_cap": sum(1 for v in per_day.values() if v >= bot.MAX_ALERTS_PER_DAY),
    }


def equal_spend(prices, alert_idx, draws=RANDOM_DRAWS, seed=RANDOM_SEED):
    """同じ金額ずつ・同じ回数だけ買うとして、平均購入価格を比べる。
    A: 通知が出た時に買う / B: 期間内に等間隔で買う / C: ランダムな時に買う(多数回の平均)"""
    n = len(alert_idx)
    m = len(prices)
    if n < MIN_ALERTS_FOR_COMPARISON or m < 2:
        return None
    cost_alert = harmonic_cost([prices[i] for i in alert_idx])
    even_idx = [round(k * (m - 1) / (n - 1)) for k in range(n)]
    cost_even = harmonic_cost([prices[i] for i in even_idx])
    rng = random.Random(seed)
    costs, cheaper = [], 0
    for _ in range(draws):
        c = harmonic_cost([prices[i] for i in rng.sample(range(m), n)])
        costs.append(c)
        if c < cost_alert:
            cheaper += 1
    cost_random = sum(costs) / len(costs)
    end_price = prices[-1]
    return {
        "n": n,
        "cost_alert": cost_alert,
        "cost_even": cost_even,
        "cost_random": cost_random,
        "diff_even": cost_alert / cost_even - 1.0,
        "diff_random": cost_alert / cost_random - 1.0,
        "random_cheaper_share": cheaper / draws,
        "end_price": end_price,
        "ret_alert": end_price / cost_alert - 1.0,
        "ret_even": end_price / cost_even - 1.0,
    }


def analyze(sim, draws=RANDOM_DRAWS):
    times, prices, zones, alerts = sim["times"], sim["prices"], sim["zones"], sim["alerts"]
    fwd = {h: forward_returns(times, prices, h * 86400) for h in HORIZONS_DAYS}
    n = len(times)

    # ゾーン別: その時点のあとの騰落率（全サンプル）
    zone_idx = {z: [i for i in range(n) if zones[i] == z] for z in ("buy", "watch", "danger")}
    known_idx = [i for z in zone_idx.values() for i in z]
    zone_stats = {}
    for z, idx in list(zone_idx.items()) + [("all", known_idx)]:
        zone_stats[z] = {
            "share": len(idx) / n if n else 0.0,
            "n": len(idx),
            **{h: summarize([fwd[h][i] for i in idx]) for h in HORIZONS_DAYS},
        }

    # 通知が出た時点
    alert_idx = [i for i in range(n) if alerts[i]]
    groups = {
        "all": alert_idx,
        "first": [i for i in alert_idx if alerts[i] == "first"],
        "followup": [i for i in alert_idx if alerts[i] == "followup"],
    }
    alert_stats = {k: {h: summarize([fwd[h][i] for i in idx]) for h in HORIZONS_DAYS}
                   for k, idx in groups.items()}

    # 通知のあと7日以内の「さらに下がった最大幅」「一度でも上がった最大幅」
    ext = [window_extremes(times, prices, i, 7 * 86400) for i in alert_idx]
    ext = [e for e in ext if e is not None]
    risk = None
    if ext:
        drops = [e[0] for e in ext]
        rises = [e[1] for e in ext]
        risk = {
            "n": len(ext),
            "drop_mean": sum(drops) / len(drops),
            "drop_worst": min(drops),
            "drop_ge3": sum(1 for d in drops if d <= -0.03) / len(drops),
            "drop_ge5": sum(1 for d in drops if d <= -0.05) / len(drops),
            "rise_ge3": sum(1 for r in rises if r >= 0.03) / len(rises),
            "rise_mean": sum(rises) / len(rises),
        }

    # 年別（日本時間）: 通知後7日の平均 と 基準（全サンプル）
    yearly = defaultdict(lambda: {"alerts_total": 0, "alerts": [], "base": []})
    for i in range(n):
        if zones[i] == "collecting" or zones[i] == "error":
            continue
        y = dt.datetime.fromtimestamp(times[i], JST).year
        yearly[y]["base"].append(fwd[7][i])
        if alerts[i]:
            yearly[y]["alerts_total"] += 1
            yearly[y]["alerts"].append(fwd[7][i])
    yearly_stats = {y: {"alerts_total": v["alerts_total"],
                        "alerts": summarize(v["alerts"]),
                        "base": summarize(v["base"])} for y, v in sorted(yearly.items())}

    return {
        "samples": n,
        "zone_stats": zone_stats,
        "freq": alert_frequency(times, alerts),
        "alert_stats": alert_stats,
        "risk": risk,
        "equal_spend": equal_spend(prices, alert_idx, draws=draws),
        "yearly": yearly_stats,
    }


def run_all(series):
    ideal = analyze(simulate(series))
    sparse = [analyze(simulate(sparse_series(series, s))) for s in SPARSE_SEEDS]
    return ideal, sparse


# ============================================================
# 偶然との見分け: 「偽の相場」でも同じルールを試す
# ============================================================
def shuffled_series(series, seed, block=NULL_BLOCK_CANDLES):
    """「偽の相場」を作る。本物の15分ごとの値動き(対数の変化率)を1日ぶんずつの塊に切り、
    塊の順番だけをシャッフルして並べ直す。
      ・値動きの大きさ、1日の中の動き方、期間全体の上げ下げ幅は本物と同じ
      ・「何日かけて下がって戻った」といった、日と日のつながり(流れ)だけが壊れる
    時刻の並び（日付の区切りやデータ欠損の位置）は本物のまま使う。"""
    n = len(series)
    if n < 3:
        return list(series)
    rets = [math.log(series[k][1] / series[k - 1][1]) for k in range(1, n)]
    blocks = [rets[i:i + block] for i in range(0, len(rets), block)]
    tail = blocks.pop() if blocks and len(blocks[-1]) < block else None   # 端数の塊は最後に置いたままにする
    random.Random(seed).shuffle(blocks)
    if tail:
        blocks.append(tail)
    out = [series[0]]
    logp = math.log(series[0][1])
    k = 1
    for b in blocks:
        for r in b:
            logp += r
            out.append((series[k][0], math.exp(logp)))
            k += 1
    return out


def key_metrics(res):
    """本物と偽の相場を、同じ物差しで比べるための数字。"""
    a7, b7 = res["alert_stats"]["all"][7], res["zone_stats"]["all"][7]
    a30, b30 = res["alert_stats"]["all"][30], res["zone_stats"]["all"][30]
    r, e = res["risk"], res["equal_spend"]
    return {
        "alerts": res["freq"]["total"],
        "edge7": (a7["mean"] - b7["mean"]) if a7 and b7 else None,
        "edge30": (a30["mean"] - b30["mean"]) if a30 and b30 else None,
        "drop_ge3": r["drop_ge3"] if r else None,
        "diff_even": e["diff_even"] if e else None,
    }


def run_null(series, paths=None, seed=NULL_SEED, log=print):
    paths = NULL_PATHS if paths is None else paths
    out = []
    for k in range(paths):
        res = analyze(simulate(shuffled_series(series, seed + k)), draws=NULL_RANDOM_DRAWS)
        out.append(key_metrics(res))
    log(f"[INFO] null paths done={len(out)}")
    return out


# ============================================================
# レポート（日本語・Markdown）
# ============================================================
def pct(x, digits=1):
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def pct_plain(x, digits=0):
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def pt(x, digits=1):
    """騰落率どうしの差（パーセントポイント）。"""
    return "—" if x is None else f"{x * 100:+.{digits}f}pt"


def count_text(x):
    return "—" if x is None else f"{x:.0f}通"


def yen(x):
    return "—" if x is None else f"{x:,.0f}円"


def _stat_cells(s):
    if not s:
        return "—", "—", "—", "0"
    return pct(s["mean"]), pct(s["median"]), pct_plain(s["win"]), f"{s['n']:,}"


def render_scenario(title, res):
    L = [f"## {title}", ""]
    f = res["freq"]
    L += [
        "### ① 通知の頻度",
        f"- 買い時通知は合計 **{f['total']}通**（最初の通知 {f['first']}通＋続報 {f['followup']}通）",
        f"- 月平均 **{f['per_month_avg']:.1f}通**／一番多かった月は {f['per_month_max']}通" if f["per_month_avg"] is not None else "- 月平均: —",
        f"- 通知が1通以上あった日: {f['days_with_alert']}日／{f['days_total']}日（1日の上限{bot.MAX_ALERTS_PER_DAY}通に達した日: {f['days_with_cap']}日）",
        "- 時間の使い方: " + "／".join(
            f"{ZONE_LABEL[z]} {pct_plain(res['zone_stats'][z]['share'], 1)}" for z in ("buy", "watch", "danger")),
        "",
    ]

    L += ["### ② 通知が出た時に買っていたら、その後どうなったか（円建ての騰落率）",
          "| 何日後 | 平均 | 中央値 | プラスだった割合 | 件数 | （比較）いつ買っても平均 | （比較）プラスだった割合 |",
          "|---|---|---|---|---|---|---|"]
    for h in HORIZONS_DAYS:
        a = res["alert_stats"]["all"][h]
        b = res["zone_stats"]["all"][h]
        am, amed, aw, an = _stat_cells(a)
        L.append(f"| {h}日後 | {am} | {amed} | {aw} | {an} | {pct(b['mean']) if b else '—'} | {pct_plain(b['win']) if b else '—'} |")
    L += ["", "※「いつ買っても」＝全部の時点で買った場合の平均（基準）。通知の方がこれより良ければ、ルールに意味がある可能性があります。", ""]

    L += ["#### 通知の種類別（最初の通知／続報＝前回通知からさらに下がった時）",
          "| 種類 | 件数 | 7日後の平均 | 7日後プラス割合 | 30日後の平均 | 30日後プラス割合 |",
          "|---|---|---|---|---|---|"]
    for label, kind in (("最初の通知", "first"), ("続報", "followup")):
        s7, s30 = res["alert_stats"][kind][7], res["alert_stats"][kind][30]
        L.append(
            f"| {label} | {f['first'] if kind == 'first' else f['followup']:,} | "
            f"{pct(s7['mean']) if s7 else '—'} | {pct_plain(s7['win']) if s7 else '—'} | "
            f"{pct(s30['mean']) if s30 else '—'} | {pct_plain(s30['win']) if s30 else '—'} |")
    L.append("")

    r = res["risk"]
    L.append("### ③ 外れパターン: 通知のあとの7日間で、さらに下がった／上がった幅")
    if r:
        L += [
            f"- 7日以内に、通知時の価格より **3%以上さらに下がった割合: {pct_plain(r['drop_ge3'])}**、5%以上: {pct_plain(r['drop_ge5'])}",
            f"- 7日以内の一番深い下げの平均: {pct(r['drop_mean'])}／最悪: {pct(r['drop_worst'])}（対象 {r['n']}通）",
            f"- 7日以内に一度でも3%以上上がった割合: {pct_plain(r['rise_ge3'])}",
        ]
    else:
        L.append("- データ不足のため算出できません")
    L.append("")

    L += ["### ④ ゾーン別: そのあとの騰落率（全部の時点）",
          "| ゾーン | 割合 | 7日後の平均 | 7日後の中央値 | 7日後プラス割合 | 30日後の平均 | 30日後プラス割合 |",
          "|---|---|---|---|---|---|---|"]
    for z in ("buy", "watch", "danger", "all"):
        zs = res["zone_stats"][z]
        label = ZONE_LABEL.get(z, "全体")
        s7, s30 = zs[7], zs[30]
        L.append(
            f"| {label} | {pct_plain(zs['share'], 1)} | {pct(s7['mean']) if s7 else '—'} | {pct(s7['median']) if s7 else '—'} | "
            f"{pct_plain(s7['win']) if s7 else '—'} | {pct(s30['mean']) if s30 else '—'} | {pct_plain(s30['win']) if s30 else '—'} |")
    L += ["", "※隣り合う時点はほぼ同じ場面の繰り返しなので、件数が多くても「独立した証拠」ではありません。", ""]

    e = res["equal_spend"]
    L.append("### ⑤ 同じ金額・同じ回数で買うなら、平均購入価格はどうだったか")
    if e:
        cheaper = "安く" if e["diff_even"] < 0 else "高く"
        L += [
            f"- 通知で買った場合（{e['n']}回）: 平均購入価格 **{yen(e['cost_alert'])}**",
            f"- 期間内に等間隔で買った場合: {yen(e['cost_even'])} → 通知買いは {abs(e['diff_even']) * 100:.1f}% {cheaper}買えた",
            f"- ランダムな時に買った場合（{RANDOM_DRAWS:,}回の平均）: {yen(e['cost_random'])} → 通知買いとの差 {pct(e['diff_random'])}",
            f"- ランダム買いの方が通知買いより安かった回数の割合: {pct_plain(e['random_cheaper_share'])}"
            "（通知は安い時期に偏って出ることがあり、それだけでもこの数字は小さくなります。偶然との見分けは下の C を優先して見てください）",
            f"- 参考: 期間の最後の価格 {yen(e['end_price'])} で評価すると、通知買い {pct(e['ret_alert'])} ／ 等間隔買い {pct(e['ret_even'])}",
        ]
    else:
        L.append(f"- 通知が{MIN_ALERTS_FOR_COMPARISON}通未満のため比較できません")
    L.append("")

    L += ["### ⑥ 年ごとのバラつき（通知後7日の平均 vs いつ買っても）",
          "| 年 | 通知数 | 通知後7日の平均 | プラス割合 | いつ買っても平均 | プラス割合 |",
          "|---|---|---|---|---|---|"]
    for y, v in res["yearly"].items():
        a, b = v["alerts"], v["base"]
        L.append(
            f"| {y} | {v['alerts_total']} | {pct(a['mean']) if a else '—'} | {pct_plain(a['win']) if a else '—'} | "
            f"{pct(b['mean']) if b else '—'} | {pct_plain(b['win']) if b else '—'} |")
    L.append("")
    return L


def render_sparse_range(sparse_runs):
    """見られ方を5通りに変えてやり直した時の、結果の幅。"""
    L = ["### 見られ方を5通りに変えてやり直した結果の幅",
         "| 指標 | 最小 | 最大 |", "|---|---|---|"]

    def rng_of(vals, fmt):
        vals = [v for v in vals if v is not None]
        return (fmt(min(vals)), fmt(max(vals))) if vals else ("—", "—")

    rows = [
        ("通知の合計数", [r["freq"]["total"] for r in sparse_runs], lambda v: f"{v:.0f}通"),
        ("月平均の通知数", [r["freq"]["per_month_avg"] for r in sparse_runs], lambda v: f"{v:.1f}通"),
        ("通知後7日の平均騰落率",
         [r["alert_stats"]["all"][7]["mean"] if r["alert_stats"]["all"][7] else None for r in sparse_runs], pct),
        ("通知後7日にプラスだった割合",
         [r["alert_stats"]["all"][7]["win"] if r["alert_stats"]["all"][7] else None for r in sparse_runs], pct_plain),
        ("通知後7日以内に3%以上さらに下がった割合",
         [r["risk"]["drop_ge3"] if r["risk"] else None for r in sparse_runs], pct_plain),
        ("平均購入価格の差（通知買い vs 等間隔買い）",
         [r["equal_spend"]["diff_even"] if r["equal_spend"] else None for r in sparse_runs], pct),
    ]
    for label, vals, fmt in rows:
        lo, hi = rng_of(vals, fmt)
        L.append(f"| {label} | {lo} | {hi} |")
    L.append("")
    return L


NULL_ROWS = (
    # (表示名, キー, 大きいほど良いか(Noneは良し悪しなし), 表示形式)
    ("通知後7日の平均 − いつ買っても7日の平均", "edge7", True, pt),
    ("通知後30日の平均 − いつ買っても30日の平均", "edge30", True, pt),
    ("通知後7日以内に3%以上さらに下がった割合", "drop_ge3", False, pct_plain),
    ("平均購入価格の差（通知買い vs 等間隔買い）", "diff_even", False, pct),
    ("通知の合計数", "alerts", None, count_text),
)


def _position(real, vals, higher_is_better):
    if real is None or len(vals) < 5:
        return "—"
    if min(vals) <= real <= max(vals):
        return "範囲の中（偶然と区別できない）"
    if higher_is_better is None:
        return "範囲の外"
    better = (real > max(vals)) == bool(higher_is_better)
    return "範囲の外（本物の方が良い）" if better else "範囲の外（本物の方が悪い）"


def render_null(real, nulls):
    L = [
        "## C. 偶然との見分け（『偽の相場』との比較）",
        f"本物の値動きを1日ぶんずつの塊に切り、塊の順番だけをシャッフルした『偽の相場』を{len(nulls)}本作って、同じルールを試しました"
        "（Aと同じ、15分ごとに見られた場合）。値動きの大きさも、期間全体の上げ下げ幅も、1日の中の動き方も本物と同じですが、"
        "「何日かけて下がって戻った」といった日と日のつながりは壊れています。",
        "本物の数字が偽の相場の範囲の中なら、「ルールが当たった」とは言えず、偶然でも出る程度の差、という意味になります。",
        "",
        "| 指標 | 本物 | 偽の相場の範囲（最小〜最大） | 本物の位置 |",
        "|---|---|---|---|",
    ]
    for label, key, higher_is_better, fmt in NULL_ROWS:
        vals = [m[key] for m in nulls if m.get(key) is not None]
        span = f"{fmt(min(vals))} 〜 {fmt(max(vals))}" if vals else "—"
        L.append(f"| {label} | {fmt(real.get(key))} | {span} | {_position(real.get(key), vals, higher_is_better)} |")
    L += ["", "※ptは「%どうしの差」の単位です（例: +1.0%と+0.5%の差は0.5pt）。",
          "※偽の相場は本数が少ないので、範囲の端ぎりぎりの結果は「外れた」と言い切れません。", ""]
    return L


def render_report(meta, ideal, sparse_runs, nulls=None):
    L = [
        "# BTC買い時ルールの過去検証",
        "",
        "※検証専用です。LINEは送らず、注文も出さず、お金も動かしません。**過去の結果は将来を保証しません。**",
        "",
        "## 0. 前提",
        f"- 期間: {meta['start']} 〜 {meta['end']}（日本時間）",
        f"- データ: bitbank BTC/JPY の15分足 {meta['rows']:,}本（取得できた割合 {meta['coverage_pct']:.1f}%、取得に失敗した日 {meta['failed_days']}日）",
        "- 価格は、本番botが見ているbitFlyerの価格ではなく、bitbankの15分足の終値で代用しています（ふつうは近い値ですが、完全には一致しません）",
        f"- 判定ルール: 本番botと同じ関数。直近{bot.LOOKBACK_HOURS:.0f}時間の平均より{abs(bot.MODES[bot.DEFAULT_MODE]['buy_threshold_pct']):.1f}%以上安い→買い時／"
        f"{bot.MODES[bot.DEFAULT_MODE]['danger_threshold_pct']:.1f}%以上高い→危険／その間は静観",
        f"- 通知ルール: 本番botと同じ関数。1日{bot.MAX_ALERTS_PER_DAY}通まで・最短{bot.NOTIFY_COOLDOWN_SECONDS // 60}分おき・"
        f"続報は前回通知価格からさらに{bot.FOLLOWUP_DROP_PCT:.1f}%以上下がった時だけ",
        "- 含まれないもの: 販売所のスプレッドや手数料／通知が届いてから実際に買うまでの遅れ／手持ちの円残高の制約／税金",
        "- 2つの見られ方で試しました: A=15分ごとに必ず価格を見られた場合（理想）／B=GitHubの自動実行が遅れて1〜5.5時間おきにしか見られない場合（実際に近い）",
        "",
    ]
    L += render_scenario("A. 理想（15分ごとに必ず見られた場合）", ideal)
    first = sparse_runs[0]
    L += render_scenario(f"B. 実際に近い（{SPARSE_MIN_GAP_H}〜{SPARSE_MAX_GAP_H}時間おきにしか見られない場合・パターン1）", first)
    L += render_sparse_range(sparse_runs)
    if nulls:
        L += render_null(key_metrics(ideal), nulls)
    L += [
        "## 読み方の注意",
        "- 同じ下落局面の中で通知が何通も出るため、通知の件数は「独立した当たり外れの回数」ではありません。",
        "- 検証した期間の相場（上げ・下げの流れ）に結果が大きく左右されます。年ごとの表で、効いた年・効かなかった年を確認してください。",
        "- しきい値（1%など）を後から探して「当たって見える」値に合わせると、将来は当たりません。本番のルールそのものの結果だけを見ています。",
        "- 押し目を待つやり方は、待っている間に相場が上がり続けると取り逃がします。この表は「買った1回ごとの得失」を見ていて、買わずに待っていた間の取り逃がしは含みません。",
        "- 販売所のスプレッド（買値と売値の差）が数%ある場合、1〜2%の押し目を狙う利益は、コストで消える可能性があります。",
    ]
    return "\n".join(L)


# ============================================================
# 実行
# ============================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="買い時ルールの過去検証（検証専用）")
    p.add_argument("--years", type=int, default=4, help="何年ぶん試すか（1〜8）")
    p.add_argument("--end", type=str, default=None, help="最後の日（UTC, YYYY-MM-DD）。省略すると昨日")
    return p.parse_args(argv)


def main(argv=None, session=None, sleep=FETCH_SLEEP_SECONDS):
    args = parse_args(argv)
    if not 1 <= args.years <= 8:
        print("[ERROR] years は 1〜8 で指定してください。")
        return 2
    if args.end:
        end_day = dt.datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        end_day = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    start_day = end_day - dt.timedelta(days=int(365.25 * args.years))

    print(f"[INFO] backtest start years={args.years} days={(end_day - start_day).days + 1}")
    try:
        series, meta = fetch_series(start_day, end_day, session=session, sleep=sleep)
    except BacktestError as e:
        print(f"[ERROR] {e}")
        return 1
    print(f"[INFO] fetched candles={meta['rows']} coverage={meta['coverage_pct']:.1f}% failed_days={meta['failed_days']}")

    ideal, sparse_runs = run_all(series)
    nulls = run_null(series)
    report = render_report(meta, ideal, sparse_runs, nulls)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(report + "\n")
        except OSError:
            print("[WARN] summary_write_failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
