"""
analyze_jquants.py
-------------------
GitHub Actions(jquants_fetch.yml)から手動/定期実行される、
日本株の複数銘柄シグナルスクリーニング & LINE通知スクリプト。

■ 全体の流れ
    WATCHLIST(監視銘柄コードのリスト)を1銘柄ずつ
      fetch_data.fetch_daily_bars() で直近データ取得
      → indicators.py の関数群でMA・RSI・出来高スパイク・
        ゴールデンクロス/デッドクロス/モメンタム転換を計算
      → 最新1営業日にシグナルが立っていれば「ヒット」として集計
    ヒットが1件以上あれば、まとめて1通のLINEメッセージで通知する
    (銘柄ごとに何通も送るとうるさい・LINEのレート制限にも引っかかりやすいため)

■ このファイルを動かす前提
    fetch_data.py と indicators.py が、この analyze_jquants.py と
    「同じフォルダ」にあること(import で読み込むため)。
    → これまで .github/workflows/ 直下にあったはずなので、
      リポジトリのルート(analyze_jquants.pyの隣)に移動しておくこと。

■ 環境変数
    JQUANTS_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
    (すべてjquants_fetch.ymlのenv:経由でSecretsから渡される想定)

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
    1銘柄分のデータを取得して指標計算し、最新営業日のシグナル状況を
    辞書で返す。データが取得できない/足りない場合はNoneを返す。
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


def format_hit_message(result: dict, name: str) -> str:
    tags = []
    if result["golden_cross_5_25"]:
        tags.append("🟢ゴールデンクロス(5×25)")
    if result["golden_cross_25_75"]:
        tags.append("🟢ゴールデンクロス(25×75)")
    if result["dead_cross_5_25"]:
        tags.append("🔴デッドクロス(5×25)")
    if result["dead_cross_25_75"]:
        tags.append("🔴デッドクロス(25×75)")
    if result["volume_spike"]:
        tags.append("📊出来高急増")
    if result["momentum_up"]:
        tags.append("⤴モメンタム上向き(RSI>50)")
    if result["rsi_overbought"]:
        tags.append("⚠買われすぎ(RSI>70)")
    if result["rsi_oversold"]:
        tags.append("🔵売られすぎ(RSI<30)")

    return (
        f"{name}({result['code'][:4]})\n"
        f"{' / '.join(tags)}\n"
        f"終値: {result['close']:,.0f}円 / RSI14: {result['rsi']:.1f}"
    )


def main():
    api_key = fetch_data.get_api_key()
    line_token, line_user_id = get_line_credentials()

    print(f"監視銘柄 {len(WATCHLIST)}件のシグナルをチェックします...")

    hit_messages = []
    for i, (code, name) in enumerate(WATCHLIST.items()):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)

        result = analyze_one_stock(code, api_key)
        if result is None:
            continue

        any_signal = any([
            result["golden_cross_5_25"], result["golden_cross_25_75"],
            result["dead_cross_5_25"], result["dead_cross_25_75"],
            result["volume_spike"], result["momentum_up"],
            result["rsi_overbought"], result["rsi_oversold"],
        ])
        print(f"  {code}({name}): {'シグナルあり' if any_signal else 'シグナルなし'} (終値{result['close']:,.0f}円 / RSI{result['rsi']:.1f})")

        if any_signal:
            hit_messages.append(format_hit_message(result, name))

    if not hit_messages:
        print("本日は条件に合致する銘柄はありませんでした。LINE通知はスキップします。")
        return

    header = f"📈 本日のシグナル検出({len(hit_messages)}件)\n" + "-" * 20 + "\n"
    body = ("\n" + "-" * 20 + "\n").join(hit_messages)
    message = header + body
    if len(message) > MAX_LINE_MESSAGE_LEN:
        message = message[:MAX_LINE_MESSAGE_LEN] + "\n…(以下省略)"

    sent = send_line_message(message, line_token, line_user_id)
    print("LINEへの送信に成功しました！" if sent else "LINE送信に失敗しました。上のDEBUG行を確認してください。")


if __name__ == "__main__":
    main()
