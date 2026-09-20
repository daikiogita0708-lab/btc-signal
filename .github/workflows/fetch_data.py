"""
fetch_data.py
-------------
「攻めの株式投資シグナルアプリ」の第一歩:
J-Quants APIから日本株の日足データを取得してCSVに保存するだけのスクリプト。

■ 前提
- https://jpx-jquants.com/ でアカウント登録し、ダッシュボードの「API Keys」画面で
  APIキーを発行済みであること(V2はリフレッシュトークン等は不要な単純なAPIキー方式)
- Freeプランは「直近12週間」のデータが取得できない(遅延配信)。
  つまりこのスクリプトで取れるのは"過去データ"のみで、当日のシグナル検知には使えない。
  → だからこそ、まずはバックテスト・学習用のデータ収集から始めるのが正しい順番。
- APIキーは環境変数 JQUANTS_API_KEY から読む。コードに直接書き込まないこと。

■ ローカルPCなしで動かす場合(GitHub Codespaces / github.dev)
    ターミナルで:
    $ export JQUANTS_API_KEY="発行されたAPIキー"
    $ pip install requests pandas --break-system-packages
    $ python fetch_data.py

■ GitHub Actionsで自動実行する場合
    リポジトリの Settings > Secrets and variables > Actions で
    JQUANTS_API_KEY をシークレット登録し、ワークフローのenvから渡す
    (bot.pyのbitFlyerキーと同じ扱い方でOK)。

■ レスポンス列名について(2026年9月に公式リファレンスで確認済み)
    https://jpx-jquants.com/ja/spec/eq-bars-daily
    V2 APIは "O/H/L/C/Vo"(調整前)と "AdjO/AdjH/AdjL/AdjC/AdjVo"(調整済み)という
    短縮キー名で四本値・出来高を返す。ここでは株式分割等があっても移動平均線が
    不連続にならないよう、調整済み株価(Adj系)を使い、indicators.py側が期待する
    Date/Open/High/Low/Close/Volume という分かりやすい列名に変換して保存する。
    (もし今後さらに仕様変更があれば、下の USE_ADJUSTED_PRICES と RESPONSE_COLUMN_MAP
    だけ直せばよい)
"""

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

API_BASE = "https://api.jquants.com/v2"
FREE_PLAN_DELAY_WEEKS = 12  # Freeプランのデータ遅延期間(公式仕様、2026年9月時点)

# 調整済み株価(分割・配当の影響を除いた連続的な価格)を使うかどうか。
# 生の始値/終値をそのまま使うと、株式分割があった銘柄でMAが不連続な
# ギザギザになり、ゴールデンクロス等が誤検知されやすいため True を既定にしている。
USE_ADJUSTED_PRICES = True

# APIレスポンスの実際のキー名 → indicators.py/backtest_signals.py が期待する分かりやすい名前
RESPONSE_COLUMN_MAP = {
    True: {"Date": "Date", "AdjO": "Open", "AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"},
    False: {"Date": "Date", "O": "Open", "H": "High", "L": "Low", "C": "Close", "Vo": "Volume"},
}


def get_api_key() -> str:
    """環境変数からAPIキーを取得する。未設定なら分かりやすいエラーで止める。"""
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        sys.exit(
            "エラー: 環境変数 JQUANTS_API_KEY が未設定です。\n"
            "  export JQUANTS_API_KEY='発行されたAPIキー' を実行してから再度試してください。"
        )
    return api_key


def _extract_records(payload: dict) -> list:
    """レスポンスJSONから実データのリストを取り出す。"""
    for key in ("data", "daily_quotes", "bars"):
        if key in payload:
            return payload[key]
    raise KeyError(f"想定したキーが見つかりません。payload.keys()={list(payload.keys())}")


def normalize_columns(df: pd.DataFrame, use_adjusted: bool = USE_ADJUSTED_PRICES) -> pd.DataFrame:
    """
    APIの短縮カラム名(O/H/L/C/Vo または AdjO/AdjH/AdjL/AdjC/AdjVo)を
    Date/Open/High/Low/Close/Volume に統一する。
    """
    rename_map = RESPONSE_COLUMN_MAP[use_adjusted]
    missing = [c for c in rename_map if c not in df.columns]
    if missing:
        sys.exit(
            f"想定したAPIレスポンスの列が見つかりません: {missing}\n"
            f"実際の列: {list(df.columns)}\n"
            f"→ J-Quants側の仕様が変わった可能性があります。公式リファレンス "
            f"(https://jpx-jquants.com/ja/spec/eq-bars-daily) を確認し、"
            f"RESPONSE_COLUMN_MAP を実際のキー名に合わせて書き換えてください。"
        )
    return df[list(rename_map.keys())].rename(columns=rename_map)


def fetch_daily_bars(code: str, from_date: str, to_date: str, api_key: str) -> pd.DataFrame:
    """
    銘柄コード(5桁、例: トヨタ自動車なら"72030")の日足四本値・出来高を取得する。
    yfinanceの"7203.T"表記とは異なるので注意。
    """
    headers = {"x-api-key": api_key}
    params = {"code": code, "from": from_date, "to": to_date}

    resp = requests.get(
        f"{API_BASE}/equities/bars/daily",
        params=params,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()  # 認証エラーなどはここで例外として止まる
    payload = resp.json()

    records = _extract_records(payload)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    return normalize_columns(df)


def main():
    api_key = get_api_key()

    target_code = "72030"  # トヨタ自動車。動作確認用に好きな銘柄コードに変更してOK
    today = datetime.today()
    to_date = (today - timedelta(weeks=FREE_PLAN_DELAY_WEEKS)).strftime("%Y%m%d")
    from_date = (today - timedelta(weeks=FREE_PLAN_DELAY_WEEKS + 52)).strftime("%Y%m%d")

    print(f"{target_code} の日足データを {from_date}〜{to_date} の範囲で取得します...")
    df = fetch_daily_bars(target_code, from_date, to_date, api_key)

    if df.empty:
        print("データが0件でした。日付範囲や銘柄コードを確認してください。")
        return

    output_path = f"data_{target_code}.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"完了: {len(df)}行を {output_path} に保存しました。")
    print(df.tail())


if __name__ == "__main__":
    main()
