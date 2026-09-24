"""
analyze_jquants.py
-------------------
GitHub Actions(jquants_fetch.yml、毎朝の自動実行)から動く、
日本の主要10銘柄の「初心者向けテクニカル分析デイリーレポート」を
LINEに届けるスクリプト。

■ コンセプト
    株の知識ゼロでも、毎日このLINE通知を読むだけで
    「今どの銘柄が買い時/売り時のサインを出しているか」が分かり、
    かつ用語の意味も自然に覚えられることを目指す。

■ 判定しているシグナル(いずれも移動平均線25日/75日とRSI14が根拠)
    ・RSI 30以下   → 売られすぎ(反発の買いチャンス候補)
    ・RSI 70以上   → 買われすぎ(高値掴み注意)
    ・ゴールデンクロス(GC) → MA25がMA75を下から上に抜けた瞬間
    ・デッドクロス(DC)     → MA25がMA75を上から下に抜けた瞬間

■ 重要: 無料プランのデータ遅延について(必ず読んでください)
    J-QuantsのFreeプランは直近12週間のデータが取得できません。
    そのためこのレポートの「最新データ」は常に実際の市場の
    約12週間前の日付になります。このレポートは技術分析の"練習"
    として使い、実際の売買はより新しい情報と自己判断で行ってください。

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

# ============ 監視銘柄(主要10銘柄) ============
# 5桁コード(普通株式は末尾0)。増減はここを書き換えるだけでよい。
WATCHLIST = {
    "94320": "NTT",
    "72030": "トヨタ自動車",
    "67580": "ソニーグループ",
    "99840": "ソフトバンクグループ",
    "83060": "三菱UFJフィナンシャルG",
    "79740": "任天堂",
    "68610": "キーエンス",
    "99830": "ファーストリテイリング",
    "40630": "信越化学工業",
    "65010": "日立製作所",
}

RSI_OVERSOLD_TH = 30    # これ以下: 売られすぎ
RSI_OVERBOUGHT_TH = 70  # これ以上: 買われすぎ

LOOKBACK_CALENDAR_DAYS = 200  # MA75計算に十分な営業日数を確保するための遡り幅
REQUEST_INTERVAL_SEC = 0.5    # 銘柄間にわずかに間隔を空け、APIのレート制限を避ける
MAX_LINE_MESSAGE_LEN = 4800   # LINEの1通あたり上限(5000文字)に対する安全マージン

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 専門用語のミニ解説(初心者向け)。メッセージの冒頭に1回だけまとめて載せる。
GLOSSARY = (
    "💡RSI(30以下/70以上): 値動きの「買われすぎ・売られすぎ」を測る指標(0〜100)。"
    "30以下は「売られすぎ→そろそろ反発するかも」、70以上は「買われすぎ→高値掴みに注意」のサインとされます。\n"
    "💡ゴールデンクロス(GC): 短期の平均線が長期の平均線を下から上に抜けること。上昇トレンドへの転換サインとされます。\n"
    "💡デッドクロス(DC): GCの逆で、短期線が長期線を上から下に抜けること。下降トレンドへの転換サインとされます。"
)


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
    df = indicators.add_golden_cross_signal(df, "MA25", "MA75")
    df = indicators.add_dead_cross_signal(df, "MA25", "MA75")

    latest = df.iloc[-1]
    rsi = latest["RSI14"]
    return {
        "code": code,
        "date": latest["Date"].date(),
        "close": latest["Close"],
        "change_pct": latest["change_pct"],
        "rsi": rsi,
        "golden_cross": bool(latest["golden_cross_MA25_MA75"]),
        "dead_cross": bool(latest["dead_cross_MA25_MA75"]),
        "rsi_oversold": bool(rsi <= RSI_OVERSOLD_TH),
        "rsi_overbought": bool(rsi >= RSI_OVERBOUGHT_TH),
    }


def signal_tags(result: dict) -> list:
    tags = []
    if result["golden_cross"]:
        tags.append("🟢GC")
    if result["dead_cross"]:
        tags.append("🔴DC")
    if result["rsi_oversold"]:
        tags.append("🔵売られすぎ")
    if result["rsi_overbought"]:
        tags.append("⚠買われすぎ")
    return tags


def is_buy_chance(result: dict) -> bool:
    """『買いチャンス候補』の定義: RSI30以下、またはゴールデンクロス発生。"""
    return result["rsi_oversold"] or result["golden_cross"]


def format_stock_line(result: dict, name: str) -> str:
    arrow = "▲" if result["change_pct"] > 0 else ("▼" if result["change_pct"] < 0 else "→")
    tags = signal_tags(result)
    tag_line = " / ".join(tags) if tags else "特になし"
    return (
        f"{name}({result['code'][:4]})\n"
        f"  終値 {result['close']:,.0f}円 ({arrow}{result['change_pct']:+.1f}%) / RSI14 {result['rsi']:.1f}\n"
        f"  シグナル: {tag_line}"
    )


def format_buy_chance_line(result: dict, name: str) -> str:
    reasons = []
    if result["rsi_oversold"]:
        reasons.append(f"RSI {result['rsi']:.1f}(売られすぎ)")
    if result["golden_cross"]:
        reasons.append("ゴールデンクロス発生")
    return f"・{name}({result['code'][:4]}): {' / '.join(reasons)}"


def build_action_advice(buy_chance_results: list, overbought_results: list) -> str:
    lines = ["★今日チェックすべきアクション"]
    if buy_chance_results:
        lines.append("・RSIが30以下の銘柄は、チャートを開いて「下げ止まって反発しかけているか」を確認してみよう")
        lines.append("・GCが出た銘柄は、出来高も同時に増えているか見てみよう(勢いの本気度がわかります)")
    else:
        lines.append("・今日は買いチャンス候補が0件でした。焦って探しにいかず、次のシグナルを待つのも立派な戦略です")
    if overbought_results:
        lines.append("・RSIが70以上の銘柄は、飛びつかず「利益確定のタイミングかも」と一度立ち止まって考えてみよう")
    lines.append("・どの銘柄も、1日の結果だけで判断せず、数日〜数週間の値動きの流れと合わせて見る癖をつけよう")
    return "\n".join(lines)


def main():
    api_key = fetch_data.get_api_key()
    line_token, line_user_id = get_line_credentials()

    print(f"監視銘柄 {len(WATCHLIST)}件のデイリーレポートを作成します...")

    stock_lines = []
    buy_chance_results = []
    overbought_results = []
    data_dates = []
    failed_names = []

    for i, (code, name) in enumerate(WATCHLIST.items()):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)

        result = analyze_one_stock(code, api_key)
        if result is None:
            failed_names.append(name)
            continue

        data_dates.append(result["date"])
        stock_lines.append(format_stock_line(result, name))

        if is_buy_chance(result):
            buy_chance_results.append((result, name))
        if result["rsi_overbought"]:
            overbought_results.append((result, name))

        print(f"  {code}({name}): 終値{result['close']:,.0f}円 / RSI{result['rsi']:.1f} / シグナル{len(signal_tags(result))}件")

    if not stock_lines:
        print("全銘柄でデータ取得に失敗しました。LINE通知はスキップします。")
        return

    data_date_str = max(data_dates).isoformat() if data_dates else "不明"
    sep = "\n" + "-" * 20 + "\n"

    # --- ① ヘッダー ---
    header = (
        f"📊 株価デイリーレポート(データ基準日: {data_date_str})\n"
        f"※無料プランのため実際の市場より約{fetch_data.FREE_PLAN_DELAY_WEEKS}週間前のデータです。"
        f"売買の練習・学習用としてご覧ください。"
    )

    # --- ② 買いチャンス候補(一番上に表示) ---
    if buy_chance_results:
        buy_chance_block = (
            f"🎯 本日の買いチャンス候補({len(buy_chance_results)}件)\n"
            + "\n".join(format_buy_chance_line(r, n) for r, n in buy_chance_results)
        )
    else:
        buy_chance_block = "🎯 本日の買いチャンス候補\n本日は該当する銘柄はありませんでした。"

    # --- ③ 用語解説 ---
    glossary_block = "📖 用語かんたん解説\n" + GLOSSARY

    # --- ④ 監視銘柄 一覧 ---
    list_block = f"📋 監視銘柄 一覧({len(stock_lines)}銘柄)\n" + sep.join(stock_lines)
    if failed_names:
        list_block += f"\n(取得失敗: {', '.join(failed_names)})"

    # --- ⑤ 今日のアクション ---
    action_block = build_action_advice(buy_chance_results, overbought_results)

    message = sep.join([header, buy_chance_block, glossary_block, list_block, action_block])
    if len(message) > MAX_LINE_MESSAGE_LEN:
        message = message[:MAX_LINE_MESSAGE_LEN] + "\n…(以下省略)"

    sent = send_line_message(message, line_token, line_user_id)
    print("LINEへの送信に成功しました！" if sent else "LINE送信に失敗しました。上のDEBUG行を確認してください。")


if __name__ == "__main__":
    main()
    