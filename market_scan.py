"""
market_scan.py
--------------
東証プライム全体から「今日目立った銘柄」を探す。
監視10銘柄の外にある"まだ知らない株"と毎日出会うためのもの。

■ API呼び出しを節約する工夫
    全銘柄の日足は「日付を指定すると全銘柄分が1回で返る」(公式仕様)。
    そこで全銘柄データは5日分だけ取る(最新日・1日前・5日前・20日前・60日前)。
    これだけで、1日/1週間/1ヶ月/3ヶ月の値動きと、売買代金の急増が分かる。
    → 無料プラン(1分5回)でも数分で終わる。

■ 流動性フィルター
    売買代金が少ない銘柄は、少しの売買で株価が大きく動き、思った値段で売れないことがある。
    初心者が手を出すと危ないので、1日の売買代金が MIN_TURNOVER_YEN 以上の銘柄だけを対象にする。
"""

import pandas as pd

import fetch_data

MIN_TURNOVER_YEN = 500_000_000   # 売買代金5億円以上
TARGET_MARKET = "プライム"
OFFSETS = {"t": 1, "t1": 2, "t5": 6, "t20": 21, "t60": 61}  # 営業日カレンダーの末尾から何番目か


def pick_dates(calendar: list) -> dict | None:
    """営業日カレンダー(昇順の日付リスト)から、取得する5日分の日付を選ぶ。"""
    if len(calendar) < max(OFFSETS.values()):
        return None
    return {k: calendar[-n] for k, n in OFFSETS.items()}


def run_market_scan(calendar: list, api_key: str) -> dict | None:
    dates = pick_dates(calendar)
    if dates is None:
        return None

    master = fetch_data.fetch_master(api_key)
    if master.empty or "MktNm" not in master.columns:
        raise KeyError("上場銘柄一覧が取得できませんでした")
    target = master[master["MktNm"] == TARGET_MARKET].set_index("Code")

    frames = {}
    for key, d in dates.items():
        df = fetch_data.fetch_market_bars(pd.Timestamp(d).strftime("%Y%m%d"), api_key)
        if df.empty:
            raise ValueError(f"{pd.Timestamp(d).date()} の全銘柄データが空でした")
        frames[key] = df.drop_duplicates(subset="Code").set_index("Code")

    latest = frames["t"]
    codes = latest.index.intersection(target.index)
    tbl = pd.DataFrame(index=codes)
    tbl["name"] = target.loc[codes, "CoName"] if "CoName" in target.columns else codes
    tbl["sector"] = target.loc[codes, "S33Nm"] if "S33Nm" in target.columns else ""
    tbl["close"] = latest.loc[codes, "Close"].astype(float)
    tbl["turnover"] = latest.loc[codes, "Turnover"].astype(float)
    for key, label in (("t1", "ret1"), ("t5", "ret5"), ("t20", "ret20"), ("t60", "ret60")):
        past = frames[key]["Close"].reindex(codes).astype(float)
        tbl[label] = (tbl["close"] / past - 1) * 100
    base_turnover = pd.concat([frames[k]["Turnover"].reindex(codes).astype(float) for k in ("t1", "t5", "t20")],
                              axis=1).median(axis=1)
    tbl["turn_ratio"] = tbl["turnover"] / base_turnover.where(base_turnover > 0)

    liquid = tbl[tbl["turnover"] >= MIN_TURNOVER_YEN].dropna(subset=["close", "ret1", "ret60"])
    if liquid.empty:
        return None

    def rows(df):
        return [{"code": c, **r} for c, r in df.to_dict("index").items()]

    return {
        "date": pd.Timestamp(dates["t"]).date(),
        "universe_size": len(liquid),
        "momentum": rows(liquid.sort_values("ret60", ascending=False).head(5)),
        "turnover_spike": rows(liquid.dropna(subset=["turn_ratio"]).sort_values("turn_ratio", ascending=False).head(3)),
        "gainers": rows(liquid.sort_values("ret1", ascending=False).head(3)),
        "losers": rows(liquid.sort_values("ret1").head(3)),
        "advance_ratio": float((liquid["ret1"] > 0).mean() * 100),
        "above_3m_ago_ratio": float((liquid["ret60"] > 0).mean() * 100),
    }


def deep_dive_candidates(scan: dict, exclude: set, n_momentum: int = 3, n_spike: int = 2) -> list:
    """深掘りする銘柄を選ぶ(3ヶ月で強い株の上位 + 売買代金急増の上位。監視銘柄と重複は除く)。"""
    picked = []
    for group, n in (("momentum", n_momentum), ("turnover_spike", n_spike)):
        count = 0
        for r in scan[group]:
            if count >= n:
                break
            if r["code"] in exclude or r["code"] in [p["code"] for p in picked]:
                continue
            picked.append({**r, "why": "3ヶ月で強い" if group == "momentum" else "売買代金急増"})
            count += 1
    return picked
