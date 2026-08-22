# -*- coding: utf-8 -*-
"""
bot.py — ビットコイン投資サポート（クラウド版 / GitHub Actions用）
======================================================================
このファイル1本で、これまでのデスクトップ版（main.py / bitflyer_api.py /
signal_logic.py / notifier.py）と同じロジックをまとめています。
GitHub Actionsが一定間隔でこのスクリプトを1回だけ実行し、そのたびに

    価格取得 → 判定 → (買い時なら)通知 → 状態保存 → 公開ページ更新

を行います。常駐プロセスではないので、あなたのiPhoneや電源は一切不要です。

■ 設定を変える場合は、下の「設定」セクションの値を書き換えてください。
■ APIキーなどの秘密情報は、GitHubリポジトリの Secrets から
  環境変数として渡されます（このファイルには一切書きません）。
■ 生成される公開ページ（public/index.html）には、価格と判定結果だけを
  載せています。残高やAPIキーなど個人情報にあたるものは一切載せません
  （残高は「買い時」通知のメッセージ本文にだけ、あなた個人のDiscord/LINEへ
  送ります）。
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

# ============================================================
# 設定（ここは自由に書き換えてOK）
# ============================================================
DEFAULT_MODE = "standard"       # conservative / standard / aggressive
PRODUCT_CODE = "BTC_JPY"
NOTIFY_COOLDOWN_SECONDS = 60 * 60  # 同じ「買い時」通知を連発しない最短間隔

STORE_PATH = Path("state/store.json")     # 価格履歴・通知状態のキャッシュ
OUTPUT_DIR = Path("public")
OUTPUT_HTML = OUTPUT_DIR / "index.html"


# ============================================================
# シグナル判定ロジック（デスクトップ版 signal_logic.py と同一の考え方）
# ============================================================
MODES = {
    "conservative": {
        "label": "慎重モード",
        "buy_threshold_pct": -5.0,
        "danger_threshold_pct": 3.0,
    },
    "standard": {
        "label": "標準モード",
        "buy_threshold_pct": -3.0,
        "danger_threshold_pct": 5.0,
    },
    "aggressive": {
        "label": "攻めモード",
        "buy_threshold_pct": -1.5,
        "danger_threshold_pct": 8.0,
    },
}

MIN_SAMPLES = 3
MIN_SPAN_HOURS = 3.0
LOOKBACK_HOURS = 24.0

ZONE_STYLE = {
    "buy": {"emoji": "🟢", "title": "買い時ゾーン", "bg": "#2ecc71", "fg": "#0b3d20"},
    "watch": {"emoji": "🟡", "title": "静観ゾーン", "bg": "#f5c518", "fg": "#4a3800"},
    "danger": {"emoji": "🔴", "title": "危険ゾーン", "bg": "#e74c3c", "fg": "#ffffff"},
    "collecting": {"emoji": "⏳", "title": "データ収集中", "bg": "#95a5a6", "fg": "#ffffff"},
    "error": {"emoji": "⚠️", "title": "通信エラー", "bg": "#7f8c8d", "fg": "#ffffff"},
}


def evaluate(current_price, current_time, history, mode_key=DEFAULT_MODE):
    """history: [(timestamp, price), ...]。current_price/current_timeとの
    比較用に、呼び出し側で直近LOOKBACK_HOURS分に絞り込んで渡す。"""
    mode = MODES.get(mode_key, MODES[DEFAULT_MODE])
    past_points = [p for (t, p) in history if t < current_time]

    span_hours = 0.0
    if past_points:
        oldest_t = min(t for (t, p) in history if t < current_time)
        span_hours = (current_time - oldest_t) / 3600.0

    if len(past_points) < MIN_SAMPLES or span_hours < MIN_SPAN_HOURS:
        remaining_h = max(0.0, MIN_SPAN_HOURS - span_hours)
        style = ZONE_STYLE["collecting"]
        reason = (
            f"まだ判定に十分なデータが集まっていません"
            f"（あと約{remaining_h:.1f}時間ほどでデータが揃います）。"
        )
        return {
            "zone": "collecting", "reason": reason, "mode_label": mode["label"],
            "deviation_pct": None, "baseline_avg": None, **style,
        }

    baseline_avg = sum(past_points) / len(past_points)
    if baseline_avg <= 0:
        baseline_avg = current_price
    deviation_pct = (current_price - baseline_avg) / baseline_avg * 100.0

    if deviation_pct <= mode["buy_threshold_pct"]:
        zone = "buy"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）より{abs(deviation_pct):.1f}%値下がりして、"
            "一時的に買いやすくなっているためです。"
        )
    elif deviation_pct >= mode["danger_threshold_pct"]:
        zone = "danger"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）より{deviation_pct:.1f}%も値上がりしていて、"
            "高値づかみになりやすいタイミングだからです。"
        )
    else:
        zone = "watch"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）から{deviation_pct:+.1f}%の範囲におさまっていて、"
            "大きな動きがない落ち着いた状況だからです。"
        )

    style = ZONE_STYLE[zone]
    return {
        "zone": zone, "reason": reason, "mode_label": mode["label"],
        "deviation_pct": deviation_pct, "baseline_avg": baseline_avg, **style,
    }


# ============================================================
# bitFlyer API（デスクトップ版 bitflyer_api.py と同一の考え方）
# ============================================================
BASE_URL = "https://api.bitflyer.com"


class BitflyerAPIError(Exception):
    pass


class BitflyerClient:
    def __init__(self, api_key=None, api_secret=None, timeout=10):
        self._api_key = (api_key or "").strip()
        self._api_secret = (api_secret or "").strip()
        self._timeout = timeout
        self._session = requests.Session()

    def has_credentials(self):
        return bool(self._api_key) and bool(self._api_secret)

    def get_ticker(self, product_code=PRODUCT_CODE):
        return self._request("GET", "/v1/ticker", params={"product_code": product_code})

    def get_balance(self):
        if not self.has_credentials():
            raise BitflyerAPIError("APIキーが設定されていません（Secretsを確認してください）")
        return self._request("GET", "/v1/me/getbalance", private=True)

    def _sign(self, method, path_with_query, body_str):
        timestamp = str(time.time())
        message = timestamp + method + path_with_query + body_str
        sign = hmac.new(self._api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": sign,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, params=None, body=None, private=False):
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        path_with_query = path + query
        url = BASE_URL + path_with_query
        body_str = json.dumps(body) if body else ""
        headers = self._sign(method, path_with_query, body_str) if private else None

        try:
            resp = self._session.request(
                method, url, headers=headers, data=body_str if body else None,
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise BitflyerAPIError(f"通信エラー: {exc}")

        if resp.status_code == 429:
            raise BitflyerAPIError("レートリミットに達しました")
        if resp.status_code == 401:
            raise BitflyerAPIError("認証に失敗しました（APIキー/シークレットを確認してください）")
        if not resp.ok:
            raise BitflyerAPIError(f"bitFlyerがエラーを返しました（HTTP {resp.status_code}）")
        try:
            return resp.json()
        except ValueError:
            raise BitflyerAPIError("応答を読み取れませんでした")


# ============================================================
# 通知（デスクトップ版 notifier.py と同一の考え方）
# ============================================================
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def send_discord(message):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=10)
        return resp.ok
    except requests.exceptions.RequestException:
        return False


def send_line(message):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messages": [{"type": "text", "text": message}]}
    try:
        resp = requests.post(LINE_BROADCAST_URL, headers=headers, json=payload, timeout=10)
        return resp.ok
    except requests.exceptions.RequestException:
        return False


def notify_all(message):
    results = {}
    if os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        results["discord"] = send_discord(message)
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip():
        results["line"] = send_line(message)
    return results


def has_any_notifier_configured():
    return bool(os.environ.get("DISCORD_WEBHOOK_URL", "").strip()) or bool(
        os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )


# ============================================================
# 状態の保存/読込（GitHub Actionsのキャッシュに載せる小さなJSON）
# ============================================================
def load_store():
    try:
        if STORE_PATH.exists():
            with open(STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("history", [])
            data.setdefault("last_zone", None)
            data.setdefault("last_notified_at", 0.0)
            return data
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return {"history": [], "last_zone": None, "last_notified_at": 0.0}


def save_store(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ============================================================
# 公開ページ（価格と判定だけを載せる。残高・鍵は載せない）
# ============================================================
def render_html(result, price, updated_at_str, mode_label):
    bg = result["bg"]
    fg = result["fg"]
    price_text = f"{price:,.0f} 円" if price is not None else "取得できませんでした"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>ビットコイン投資サポート</title>
<style>
  body {{
    margin: 0; padding: 24px 16px 40px;
    background: #f4f5f7;
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic UI", sans-serif;
    color: #1a1a1a;
  }}
  .card {{
    max-width: 480px; margin: 0 auto;
    background: {bg}; color: {fg};
    border-radius: 16px; padding: 28px 20px; text-align: center;
    box-shadow: 0 2px 10px rgba(0,0,0,0.08);
  }}
  .emoji {{ font-size: 44px; }}
  .title {{ font-size: 24px; font-weight: 700; margin: 6px 0 10px; }}
  .reason {{ font-size: 15px; line-height: 1.6; }}
  .price {{
    max-width: 480px; margin: 16px auto 0; background: #fff;
    border-radius: 12px; padding: 16px 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  .price .label {{ font-size: 12px; color: #666; }}
  .price .value {{ font-size: 28px; font-weight: 700; margin-top: 2px; }}
  .meta {{
    max-width: 480px; margin: 10px auto 0; font-size: 12px; color: #888; text-align: center;
  }}
  .disclaimer {{
    max-width: 480px; margin: 18px auto 0; font-size: 11px; color: #999;
    line-height: 1.6; text-align: left;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="emoji">{result["emoji"]}</div>
    <div class="title">【{result["title"]}】</div>
    <div class="reason">{result["reason"]}</div>
  </div>
  <div class="price">
    <div class="label">現在のBTC価格</div>
    <div class="value">{price_text}</div>
  </div>
  <div class="meta">判定モード: {mode_label} ／ 最終更新: {updated_at_str}</div>
  <div class="disclaimer">
    ※この判定は直近の価格からの簡易的な目安であり、将来の値動きを保証するものではありません。
    投資判断はご自身の責任で行ってください。自動売買（発注）は行いません。
    このページには価格と判定のみを表示しており、残高やAPIキーは表示していません。
  </div>
</body>
</html>
"""


# ============================================================
# メイン処理
# ============================================================
def main():
    mode_key = os.environ.get("DEFAULT_MODE", DEFAULT_MODE).strip() or DEFAULT_MODE
    if mode_key not in MODES:
        mode_key = DEFAULT_MODE

    client = BitflyerClient(
        api_key=os.environ.get("BITFLYER_API_KEY"),
        api_secret=os.environ.get("BITFLYER_API_SECRET"),
    )

    store = load_store()
    now = time.time()

    price = None
    result = None
    try:
        ticker = client.get_ticker(PRODUCT_CODE)
        price = float(ticker["ltp"])
    except (BitflyerAPIError, KeyError, ValueError, TypeError) as e:
        print(f"[WARN] 価格取得に失敗: {e}")

    if price is not None:
        store["history"].append([now, price])

    cutoff = now - LOOKBACK_HOURS * 3600
    store["history"] = [[t, p] for [t, p] in store["history"] if t >= cutoff]

    if price is not None:
        history_tuples = [(t, p) for [t, p] in store["history"]]
        result = evaluate(price, now, history_tuples, mode_key)
        print(f"[INFO] price={price:,.0f} zone={result['zone']} reason={result['reason']}")
    else:
        style = ZONE_STYLE["error"]
        result = {
            "zone": "error", "reason": "bitFlyerから価格を取得できませんでした。しばらくして自動的に再試行されます。",
            "mode_label": MODES[mode_key]["label"], "deviation_pct": None, "baseline_avg": None,
            **style,
        }

    # 「買い時」に新しく入った、またはクールダウンが明けている場合だけ通知する
    if result["zone"] == "buy":
        zone_changed = store.get("last_zone") != "buy"
        cooldown_ok = (now - store.get("last_notified_at", 0.0)) >= NOTIFY_COOLDOWN_SECONDS
        if zone_changed or cooldown_ok:
            balance_line = ""
            if client.has_credentials():
                try:
                    balances = client.get_balance()
                    jpy = next((b["available"] for b in balances if b.get("currency_code") == "JPY"), None)
                    btc = next((b["available"] for b in balances if b.get("currency_code") == "BTC"), None)
                    if jpy is not None and btc is not None:
                        balance_line = f"\n残高: {jpy:,.0f}円 / {btc:.8f} BTC"
                except BitflyerAPIError as e:
                    print(f"[WARN] 残高取得に失敗（通知は残高なしで送信します）: {e}")

            if has_any_notifier_configured():
                message = (
                    f"★買い時です！\n"
                    f"現在価格: {price:,.0f}円\n"
                    f"{result['reason']}"
                    f"{balance_line}\n"
                    f"（判定モード: {result['mode_label']}）"
                )
                notify_all(message)
            store["last_notified_at"] = now

    store["last_zone"] = result["zone"]
    save_store(store)

    # 公開ページを書き出す（価格と判定のみ。残高やキーは含めない）
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Actionsランナーの時刻はUTCなので、+9時間してJST表記にする
    jst = time.gmtime(now + 9 * 3600)
    updated_at_str = time.strftime("%Y-%m-%d %H:%M JST", jst)
    html = render_html(result, price, updated_at_str, MODES[mode_key]["label"])
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    print(f"[INFO] public/index.html を書き出しました（zone={result['zone']}）")


if __name__ == "__main__":
    main()
