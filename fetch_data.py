"""
fetch_data.py
-------------
J-Quants API(V2)から日本株の日足データを取得する、APIクライアント部分。
analyze_jquants.py からも import して使う。単体で実行するとCSVに保存する。

■ 前提
- https://jpx-jquants.com/ でアカウント登録し、ダッシュボードの「API Keys」画面で
  APIキーを発行済みであること(V2はリフレッシュトークン等は不要な単純なAPIキー方式)
- APIキーは環境変数 JQUANTS_API_KEY から読む。コードに直接書き込まないこと。

■ プランによる違い(2026年10月時点の公式仕様)
    プラン      月額      データ遅延   レート制限
    Free        0円       12週間       5回/分
    Light       1,650円   なし         60回/分
    Standard    3,300円   なし         120回/分
    → 有料プランに切り替えた場合は、コードを書き換えずに環境変数だけで対応できる:
        JQUANTS_DELAY_WEEKS=0          (遅延なし)
        JQUANTS_MIN_INTERVAL_SEC=1.1   (60回/分に合わせた間隔)

■ レート制限について(重要)
    Freeプランは「1分あたり5回」まで。超えると 429 Too Many Requests が返り、
    429になったリクエストも回数に数えられるので、連打すると余計に回復が遅れる。
    そのためこのモジュールでは、全リクエストの間隔を自動で MIN_REQUEST_INTERVAL_SEC
    以上空け、万一429が返ってきたら1分以上待ってから再試行する。

■ レスポンス列名について(2026年9月に公式リファレンスで確認済み)
    https://jpx-jquants.com/ja/spec/eq-bars-daily
    V2 APIは "O/H/L/C/Vo"(調整前)と "AdjO/AdjH/AdjL/AdjC/AdjVo"(調整済み)という
    短縮キー名で四本値・出来高を返す。株式分割等で移動平均線が不連続にならないよう
    調整済み株価(Adj系)を使い、Date/Open/High/Low/Close/Volume に変換して返す。

■ 単体での実行方法(GitHub Codespaces等)
    $ export JQUANTS_API_KEY="発行されたAPIキー"
    $ python fetch_data.py
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

API_BASE = "https://api.jquants.com/v2"

# データ遅延(週)。Freeは12週。有料プランなら環境変数で0にする。
DELAY_WEEKS = int(os.environ.get("JQUANTS_DELAY_WEEKS", "12"))
FREE_PLAN_DELAY_WEEKS = DELAY_WEEKS  # 旧名。他ファイルとの互換のため残している

# リクエスト間隔(秒)。Free(5回/分)なら12秒以上必要なので、余裕を見て13秒。
MIN_REQUEST_INTERVAL_SEC = float(os.environ.get("JQUANTS_MIN_INTERVAL_SEC", "13"))
RATE_LIMIT_WAIT_SEC = 65   # 429を受けたら1分以上待つ(公式は1〜2分の待機を推奨)
MAX_RETRIES_ON_429 = 2
INTERVAL_STEP_AFTER_429_SEC = 2.0   # 429を受けたら、以後のリクエスト間隔を少し広げる(同じ失敗の繰り返しを防ぐ)
MAX_INTERVAL_SEC = 30.0
RUN_DEADLINE_SEC = float(os.environ.get("RUN_DEADLINE_SEC", "1200"))  # 20分。Actionsの上限25分より手前でデータ取得を打ち切り、取れた分でレポートを送る
_run_started_at = time.monotonic()  # スクリプトを動かし始めた時刻
MAX_PAGES = 20             # ページネーションの無限ループ防止

USE_ADJUSTED_PRICES = True

RESPONSE_COLUMN_MAP = {
    True: {"Date": "Date", "AdjO": "Open", "AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"},
    False: {"Date": "Date", "O": "Open", "H": "High", "L": "Low", "C": "Close", "Vo": "Volume"},
}

_last_request_at = None  # 直前にリクエストを送った時刻(time.monotonic)


def get_api_key() -> str:
    """環境変数からAPIキーを取得する。未設定なら分かりやすいエラーで止める。"""
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        sys.exit(
            "エラー: 環境変数 JQUANTS_API_KEY が未設定です。\n"
            "  export JQUANTS_API_KEY='発行されたAPIキー' を実行してから再度試してください。"
        )
    return api_key


def _api_error_message(resp) -> str:
    """HTTPエラー時にAPIが返す理由(messageなど)を短く取り出す。ログで原因をすぐ特定するために使う。"""
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
        return str(msg if msg else body)[:300]
    except ValueError:
        return (resp.text or "")[:300]


def _http_error(resp, params: dict) -> requests.HTTPError:
    """APIの返答(理由)と、どんな条件で呼んだかを含めた例外を作る。"""
    shown = "&".join(f"{k}={v}" for k, v in params.items() if k != "pagination_key")
    return requests.HTTPError(
        f"{resp.status_code} {resp.reason}: {_api_error_message(resp)} (条件: {shown})", response=resp)


def _throttled_get(url: str, params: dict, headers: dict) -> dict:
    """
    レート制限を守りながらGETする。
    - 前回のリクエストから MIN_REQUEST_INTERVAL_SEC 経っていなければ待つ
    - 429が返ったら RATE_LIMIT_WAIT_SEC 待って再試行(最大 MAX_RETRIES_ON_429 回)。
      同時に以後のリクエスト間隔も少し広げて、同じ失敗を繰り返さないようにする
    - それ以外のエラーは、APIが返した理由(message)つきの例外にする
    """
    global _last_request_at, MIN_REQUEST_INTERVAL_SEC
    resp = None
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        if time.monotonic() - _run_started_at > RUN_DEADLINE_SEC:
            raise TimeoutError(f"実行開始から{RUN_DEADLINE_SEC / 60:.0f}分を超えたため、これ以上のデータ取得を打ち切ります")
        if _last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)

        resp = requests.get(url, params=params, headers=headers, timeout=30)
        _last_request_at = time.monotonic()

        if resp.status_code != 429:
            if resp.status_code >= 400:
                raise _http_error(resp, params)  # 認証エラー・期間の指定ミス等
            return resp.json()

        MIN_REQUEST_INTERVAL_SEC = min(MIN_REQUEST_INTERVAL_SEC + INTERVAL_STEP_AFTER_429_SEC, MAX_INTERVAL_SEC)
        if attempt == MAX_RETRIES_ON_429:
            break
        print(f"[WARN] レート制限(429)に達しました。{RATE_LIMIT_WAIT_SEC}秒待って再試行します"
              f"({attempt + 1}/{MAX_RETRIES_ON_429})。以後のリクエスト間隔を{MIN_REQUEST_INTERVAL_SEC:.0f}秒に広げます")
        time.sleep(RATE_LIMIT_WAIT_SEC)

    raise _http_error(resp, params)


def _extract_records(payload: dict) -> list:
    """レスポンスJSONから実データのリストを取り出す。"""
    for key in ("data", "daily_quotes", "bars"):
        if key in payload:
            return payload[key]
    raise KeyError(f"想定したキーが見つかりません。payload.keys()={list(payload.keys())}")


def normalize_columns(df: pd.DataFrame, use_adjusted: bool = USE_ADJUSTED_PRICES) -> pd.DataFrame:
    """APIの短縮カラム名を Date/Open/High/Low/Close/Volume に統一する。"""
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


_ALLOWED_RANGE_RE = re.compile(
    r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})\s*(?:~|〜|～|to|-)\s*(\d{4})[-/]?(\d{2})[-/]?(\d{2})")


def _allowed_range(msg: str):
    """400のメッセージ('Your subscription covers the following dates: 2024-07-29 ~ 2026-07-29' など)から
    プランで取得できる期間(YYYYMMDD, YYYYMMDD)を取り出す。読み取れなければ None。"""
    m = _ALLOWED_RANGE_RE.search(msg or "")
    if not m:
        return None
    g = m.groups()
    lo, hi = "".join(g[0:3]), "".join(g[3:6])
    try:
        datetime.strptime(lo, "%Y%m%d")
        datetime.strptime(hi, "%Y%m%d")
    except ValueError:
        return None
    return lo, hi


def fetch_daily_bars(code: str, from_date: str, to_date: str, api_key: str) -> pd.DataFrame:
    """
    銘柄コード(5桁、例: トヨタ自動車なら"72030")の日足四本値・出来高を取得する。
    レスポンスが複数ページに分かれた場合(pagination_key)も最後まで取得する。

    期間がプランの提供範囲(Freeは「12週間前〜2年12週間前」)を超えると、APIは400を返す。
    その場合は次の順で1回だけやり直す(公式FAQ: 期間を省略するとプランの範囲に自動調整される)。
      (1) エラーメッセージに書かれた提供範囲に合わせて、期間を狭めて再取得
      (2) 範囲が読み取れなければ、期間を省略して再取得
    """
    url = f"{API_BASE}/equities/bars/daily"
    try:
        records = _get_all_pages(url, {"code": code, "from": from_date, "to": to_date}, api_key)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if resp is None or resp.status_code != 400:
            raise
        reason = _api_error_message(resp)
        allowed = _allowed_range(reason)
        new_from = new_to = ""
        if allowed:
            # 提供範囲の境界ぎりぎりで弾かれないよう、内側に1日ずつ寄せる
            inner_lo = (datetime.strptime(allowed[0], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
            inner_hi = (datetime.strptime(allowed[1], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            new_from, new_to = max(from_date, inner_lo), min(to_date, inner_hi)
        if allowed and new_from <= new_to:
            print(f"[INFO] {code}: 取得期間がプランの提供範囲({allowed[0]}〜{allowed[1]})を超えていたので、"
                  f"{new_from}〜{new_to}に合わせて再取得します")
            retry_params = {"code": code, "from": new_from, "to": new_to}
        else:
            print(f"[INFO] {code}: 期間指定で400が返りました({reason[:80]})。"
                  f"期間を省略して(プランの範囲に自動調整されます)再取得します")
            retry_params = {"code": code}
        records = _get_all_pages(url, retry_params, api_key)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    df = normalize_columns(df)
    return df.drop_duplicates(subset="Date").reset_index(drop=True)


def _get_all_pages(url: str, params: dict, api_key: str) -> list:
    """pagination_key をたどって全ページ分のレコードを集める(同じキーが返り続けても無限ループしない)。"""
    headers = {"x-api-key": api_key}
    records, seen_keys = [], set()
    for _ in range(MAX_PAGES):
        payload = _throttled_get(url, params, headers)
        records.extend(_extract_records(payload))
        next_key = payload.get("pagination_key")
        if not next_key or next_key in seen_keys:
            break
        seen_keys.add(next_key)
        params = {**params, "pagination_key": next_key}
    return records


def fetch_market_bars(date: str, api_key: str) -> pd.DataFrame:
    """
    指定日の『全上場銘柄』の日足を取得する(codeを指定せずdateだけ指定すると全銘柄が返る。公式仕様で確認済み)。
    市場全体スキャン用なので、必要な列(Code/調整後終値/調整後出来高/売買代金)だけ返す。
    """
    records = _get_all_pages(f"{API_BASE}/equities/bars/daily", {"date": date}, api_key)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    need = {"Code": "Code", "Date": "Date", "AdjC": "Close", "AdjVo": "Volume", "Va": "Turnover"}
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise KeyError(f"全銘柄データに想定した列がありません: {missing} / 実際: {list(df.columns)}")
    return df[list(need)].rename(columns=need)


def fetch_master(api_key: str) -> pd.DataFrame:
    """
    上場銘柄一覧(銘柄コード・会社名・市場区分・業種)を取得する。
    フィールド名は公式リファレンス(https://jpx-jquants.com/ja/spec/eq-master)で確認済み。
    """
    records = _get_all_pages(f"{API_BASE}/equities/master", {}, api_key)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    cols = [c for c in ("Code", "CoName", "MktNm", "S33Nm") if c in df.columns]
    return df[cols].drop_duplicates(subset="Code", keep="last")


def latest_available_date() -> datetime:
    """プランのデータ遅延を考慮した『取得できる最新日』。"""
    return datetime.today() - timedelta(weeks=DELAY_WEEKS)


def main():
    api_key = get_api_key()

    target_code = "72030"  # トヨタ自動車。動作確認用に好きな銘柄コードに変更してOK
    to_dt = latest_available_date()
    from_dt = to_dt - timedelta(weeks=52)
    from_date, to_date = from_dt.strftime("%Y%m%d"), to_dt.strftime("%Y%m%d")

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
