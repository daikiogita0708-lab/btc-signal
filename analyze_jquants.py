"""
analyze_jquants.py
-------------------
GitHub Actions(jquants_fetch.yml、毎朝の自動実行)から動く、
日本株の複数銘柄「デイリーレポート」をLINEに届けるスクリプト。

■ 全体の流れ
    WATCHLIST(監視銘柄コードのリスト)を1銘柄ずつ
      fetch_data.fetch_daily_bars() で直近データ取得
      → indicators.py の関数群でMA・RSI・出来高スパイク・
        ゴールデンクロス/デッドクロス/モメンタム転換を計算
    → 「シグナルが出た銘柄だけ」ではなく「監視銘柄全部」の状況を
      毎日1通のLINEメッセージにまとめて通知する(デイリーレポート形式)

■ 重要: 無料プランのデータ遅延について(必ず読んでください)
    J-QuantsのFreeプランは直近12週間のデータが取得できません。
    そのため、このレポートに載る「最新データ」は常に実際の市場の
    約12週間前の日付になります。メッセージの先頭に必ずその日付を
    表示しているので、「これは過去の検証用データであって、
    今日の売買判断にそのまま使えるものではない」ことを毎回確認してください。

■ このファイルを動かす前提
    fetch_data.py と indicators.py が、この analyze_jquants.py と
    同じフォルダ(リポジトリのルート直下)にあること。

■ 環境変数
    JQUANTS_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID

■ 実行方法(ローカル/Codespaces)
    $ python analyze_jquants.py
"""

import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import fetch_data
import indicators

# ============ 監視銘柄 ============
# 5桁コード(普通株式は末尾0)。増減はここを書き換えるだけでよい。
WATCHLIST = {
    "72030": "トヨタ自動車",
    "67580": "ソニーグループ",
    "99840": "ソフトバンクグループ",
}

LOOKBACK_CALENDAR_DAYS = 200  # MA75計算に十分な営業日数を確保するための遡り幅
REQUEST_INTERVAL_SEC = 0.5    # 銘柄間にわずかに間隔を空け、APIのレート制限を避ける
MAX_LINE_MESSAGE_LEN = 4800   # LINEの1通あたり上限(5000文字)に対する安全マージン

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def get_line_credentials() -> tuple[str, str]:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        sys.exit("エラー: LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が設定されていません。")
    return token, user_id


def send_line_message(message: str, token: str, user_id: str) -> bool:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"to": user_id, "messages": [{"type": "text", "text": message}]}
    resp = requests.post(LINE_PUSH_URL, headers=headers, json=data, timeout=10)
    print(f"[DEBUG] LINE送信結果: {resp.status_code} {resp.text[:200]}")
    return resp.status_code == 200


def analyze_one_stock(code: str, api_key: str) -> dict | None:
    """
    1銘柄分のデータを取得して指標計算し、最新営業日の状況を辞書で返す。
    データが取得できない/足りない場合はNoneを返す。
    """
    to_date_dt = datetime.today() - timedelta(weeks=fetch_data.FREE_PLAN_DELAY_WEEKS)
    from_date_dt = to_date_dt - timedelta(days=LOOKBACK_CALENDAR_DAYS)

    try:
        df = fetch_data.fetch_daily_bars(
            code, from_date_dt.strftime("%Y%m%d"), to_date_dt.strftime("%Y%m%d"), api_key
        )
    except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で全体を止めたくないため広めに捕捉
        print(f"[WARN] {code}: データ取得に失敗しました: {e}")
        return None

    if df.empty or len(df) < 76:  # MA75が計算できる最低日数
        print(f"[WARN] {code}: データが不足しています({len(df)}行)。スキップします。")
        return None

    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    df = indicators.add_moving_averages(df, "Close")
    df = indicators.add_rsi(df, "Close")
    df = indicators.add_daily_change(df, "Close")
    df = indicators.add_volume_spike_signal(df, "Volume")
    df = indicators.add_golden_cross_signal(df, "MA5", "MA25")
    df = indicators.add_golden_cross_signal(df, "MA25", "MA75")
    df = indicators.add_dead_cross_signal(df, "MA5", "MA25")
    df = indicators.add_dead_cross_signal(df, "MA25", "MA75")
    df = indicators.add_threshold_cross_signal(df, "RSI14", 50, "up")    # モメンタム上向き転換
    df = indicators.add_threshold_cross_signal(df, "RSI14", 70, "up")    # 過熱(買われすぎ)入り
    df = indicators.add_threshold_cross_signal(df, "RSI14", 30, "down")  # 底値圏(売られすぎ)入り

    latest = df.iloc[-1]
    return {
        "code": code,
        "date": latest["Date"].date(),
        "close": latest["Close"],
        "change_pct": latest["change_pct"],
        "rsi": latest["RSI14"],
        "golden_cross_5_25": bool(latest["golden_cross_MA5_MA25"]),
        "golden_cross_25_75": bool(latest["golden_cross_MA25_MA75"]),
        "dead_cross_5_25": bool(latest["dead_cross_MA5_MA25"]),
        "dead_cross_25_75": bool(latest["dead_cross_MA25_MA75"]),
        "volume_spike": bool(latest["volume_spike"]),
        "momentum_up": bool(latest["RSI14_cross_up_50"]),
        "rsi_overbought": bool(latest["RSI14_cross_up_70"]),
        "rsi_oversold": bool(latest["RSI14_cross_down_30"]),
    }


def signal_tags(result: dict) -> list:
    tags = []
    if result["golden_cross_5_25"]:
        tags.append("🟢GC(5×25)")
    if result["golden_cross_25_75"]:
        tags.append("🟢GC(25×75)")
    if result["dead_cross_5_25"]:
        tags.append("🔴DC(5×25)")
    if result["dead_cross_25_75"]:
        tags.append("🔴DC(25×75)")
    if result["volume_spike"]:
        tags.append("📊出来高急増")
    if result["momentum_up"]:
        tags.append("⤴モメンタム上向き")
    if result["rsi_overbought"]:
        tags.append("⚠買われすぎ")
    if result["rsi_oversold"]:
        tags.append("🔵売られすぎ")
    return tags


def format_stock_line(result: dict, name: str) -> str:
    arrow = "▲" if result["change_pct"] > 0 else ("▼" if result["change_pct"] < 0 else "→")
    tags = signal_tags(result)
    tag_line = " / ".join(tags) if tags else "特になし"
    return (
        f"{name}({result['code'][:4]})\n"
        f"  終値 {result['close']:,.0f}円 ({arrow}{result['change_pct']:+.1f}%) / RSI14 {result['rsi']:.1f}\n"
        f"  シグナル: {tag_line}"
    )


def main():
    api_key = fetch_data.get_api_key()
    line_token, line_user_id = get_line_credentials()

    print(f"監視銘柄 {len(WATCHLIST)}件のデイリーレポートを作成します...")

    lines = []
    data_dates = []
    hit_count = 0
    failed_names = []

    for i, (code, name) in enumerate(WATCHLIST.items()):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)

        result = analyze_one_stock(code, api_key)
        if result is None:
            failed_names.append(name)
            continue

        data_dates.append(result["date"])
        if signal_tags(result):
            hit_count += 1

        print(f"  {code}({name}): 終値{result['close']:,.0f}円 / RSI{result['rsi']:.1f} / シグナル{len(signal_tags(result))}件")
        lines.append(format_stock_line(result, name))

    if not lines:
        print("全銘柄でデータ取得に失敗しました。LINE通知はスキップします。")
        return

    data_date_str = max(data_dates).isoformat() if data_dates else "不明"
    header = (
        f"📊 株価デイリーレポート(データ基準日: {data_date_str})\n"
        f"※無料プランのため実際の市場より約{fetch_data.FREE_PLAN_DELAY_WEEKS}週間前のデータです\n"
        f"要注目シグナル: {hit_count}/{len(lines)}銘柄\n"
        + "-" * 20
    )
    footer = ""
    if failed_names:
        footer = "\n" + "-" * 20 + f"\n取得失敗: {', '.join(failed_names)}"

    message = header + "\n" + ("\n" + "-" * 20 + "\n").join(lines) + footer
    if len(message) > MAX_LINE_MESSAGE_LEN:
        message = message[:MAX_LINE_MESSAGE_LEN] + "\n…(以下省略)"

    sent = send_line_message(message, line_token, line_user_id)
    print("LINEへの送信に成功しました！" if sent else "LINE送信に失敗しました。上のDEBUG行を確認してください。")


if __name__ == "__main__":
