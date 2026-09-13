# -*- coding: utf-8 -*-
"""
bot.py — ビットコイン全自動売買＆学びサポート（クラウド版 / GitHub Actions用）
======================================================================
1時間ごとに価格をチェックし、一時的な下落（押し目）を捉えて
自動で「1回1,000円分」のビットコインを買い付け、LINEへ通知します。
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

# ============================================================
# 設定
# ============================================================
DEFAULT_MODE = "aggressive"         # 攻めモード（押し目を敏感に狙う）
PRODUCT_CODE = "BTC_JPY"
BUY_AMOUNT_JPY = 1000               # 1回の自動買い付け金額（1,000円固定）
NOTIFY_COOLDOWN_SECONDS = 60 * 60   # 通知・連続購入の最短間隔（1時間）

STORE_PATH = Path("state/store.json")
OUTPUT_DIR = Path("public")
OUTPUT_HTML = OUTPUT_DIR / "index.html"


# ============================================================
# シグナル判定ロジック
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
        "label": "意味のある攻めモード",
        "buy_threshold_pct": -1.0,  # 直近平均から-1%下がったら押し目と判定して買う
        "danger_threshold_pct": 8.0,
    },
}

MIN_SAMPLES = 2
MIN_SPAN_HOURS = 1.0
LOOKBACK_HOURS = 24.0

ZONE_STYLE = {
    "buy": {"emoji": "🟢", "title": "買い時ゾーン（自動買付）", "bg": "#2ecc71", "fg": "#0b3d20"},
    "watch": {"emoji": "🟡", "title": "静観ゾーン", "bg": "#f5c518", "fg": "#4a3800"},
    "danger": {"emoji": "🔴", "title": "危険ゾーン", "bg": "#e74c3c", "fg": "#ffffff"},
    "collecting": {"emoji": "⏳", "title": "データ収集中", "bg": "#95a5a6", "fg": "#ffffff"},
    "error": {"emoji": "⚠️", "title": "通信エラー", "bg": "#7f8c8d", "fg": "#ffffff"},
}


def evaluate(current_price, current_time, history, mode_key=DEFAULT_MODE):
    mode = MODES.get(mode_key, MODES[DEFAULT_MODE])
    past_points = [p for (t, p) in history if t < current_time]

    span_hours = 0.0
    if past_points:
        oldest_t = min(t for (t, p) in history if t < current_time)
        span_hours = (current_time - oldest_t) / 3600.0

    if len(past_points) < MIN_SAMPLES or span_hours < MIN_SPAN_HOURS:
        remaining_h = max(0.0, MIN_SPAN_HOURS - span_hours)
        style = ZONE_STYLE["collecting"]
        reason = f"まだ判定データが集まっていません（あと約{remaining_h:.1f}時間）。"
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
            f"直近平均（{baseline_avg:,.0f}円）より{abs(deviation_pct):.1f}%値下がりした「押し目」を検知しました。"
        )
    elif deviation_pct >= mode["danger_threshold_pct"]:
        zone = "danger"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）より{deviation_pct:.1f}%高く、高値掴みのリスクがあるため見送ります。"
        )
    else:
        zone = "watch"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）からのブレが{deviation_pct:+.1f}%の範囲内のため静観します。"
        )

    style = ZONE_STYLE[zone]
    return {
        "zone": zone, "reason": reason, "mode_label": mode["label"],
        "deviation_pct": deviation_pct, "baseline_avg": baseline_avg, **style,
    }


# ============================================================
# bitFlyer API（注文処理を追加）
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
            raise BitflyerAPIError("APIキーが設定されていません")
        return self._request("GET", "/v1/me/getbalance", private=True)

    # 成行買い注文の送信機能
    def send_buy_order(self, amount_btc, product_code=PRODUCT_CODE):
        if not self.has_credentials():
            raise BitflyerAPIError("APIキーが設定されていないため注文できません")
        
        body = {
            "product_code": product_code,
            "child_order_type": "MARKET",  # 成行注文
            "side": "BUY",                # 買い
            "size": round(amount_btc, 8)  # BTC数量（少数第8位まで）
        }
        return self._request("POST", "/v1/me/sendchildorder", body=body, private=True)

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
            raise BitflyerAPIError("認証に失敗しました（APIキーを確認してください）")
        if not resp.ok:
            raise BitflyerAPIError(f"bitFlyerエラー（HTTP {resp.status_code}）: {resp.text}")
        try:
            return resp.json()
        except ValueError:
            raise BitflyerAPIError("応答を読み取れませんでした")


# ============================================================
# 通知
# ============================================================
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


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
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip():
        send_line(message)


# ============================================================
# 状態保存/読込
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
<title>ビットコイン自動売買（攻めモード）</title>
<style>
  body {{ margin: 0; padding: 24px 16px; background: #f4f5f7; font-family: sans-serif; color: #1a1a1a; }}
  .card {{ max-width: 480px; margin: 0 auto; background: {bg}; color: {fg}; border-radius: 16px; padding: 28px 20px; text-align: center; }}
  .emoji {{ font-size: 44px; }}
  .title {{ font-size: 24px; font-weight: 700; margin: 6px 0 10px; }}
  .reason {{ font-size: 15px; line-height: 1.6; }}
  .price {{ max-width: 480px; margin: 16px auto 0; background: #fff; border-radius: 12px; padding: 16px 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); text-align: center; }}
  .price .label {{ font-size: 12px; color: #666; }}
  .price .value {{ font-size: 28px; font-weight: 700; margin-top: 2px; }}
  .meta {{ max-width: 480px; margin: 10px auto 0; font-size: 12px; color: #888; text-align: center; }}
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
</body>
</html>
"""


# ============================================================
# メイン処理
# ============================================================
def main():
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
            "zone": "error", "reason": "価格を取得できませんでした。",
            "mode_label": MODES[mode_key]["label"], "deviation_pct": None, "baseline_avg": None, **style,
        }

    # 「買い時ゾーン」の場合のみ自動買付を実施
    if result["zone"] == "buy":
        cooldown_ok = (now - store.get("last_notified_at", 0.0)) >= NOTIFY_COOLDOWN_SECONDS
        if cooldown_ok:
            # 1,000円分のBTC数量を計算
            btc_amount = BUY_AMOUNT_JPY / price
            order_success = False
            order_msg = ""

            try:
                # 自動注文を発注
                res = client.send_buy_order(btc_amount)
                order_success = True
                order_msg = f"【自動購入完了】\n{BUY_AMOUNT_JPY:,}円分（約 {btc_amount:.6f} BTC）の買い注文を発注しました！"
            except BitflyerAPIError as e:
                order_msg = f"【自動購入エラー】\n買い条件に達しましたが、注文時にエラーが発生しました: {e}"

            # 最新残高の取得
            balance_line = ""
            if client.has_credentials():
                try:
                    balances = client.get_balance()
                    jpy = next((b["available"] for b in balances if b.get("currency_code") == "JPY"), None)
                    btc = next((b["available"] for b in balances if b.get("currency_code") == "BTC"), None)
                    if jpy is not None and btc is not None:
                        balance_line = f"\n残高: {jpy:,.0f}円 / {btc:.8f} BTC"
                except BitflyerAPIError:
                    pass

            message = (
                f"🚀 {order_msg}\n"
                f"現在価格: {price:,.0f}円\n"
                f"理由: {result['reason']}"
                f"{balance_line}\n"
                f"（判定モード: {result['mode_label']}）"
            )
            notify_all(message)
            store["last_notified_at"] = now

    store["last_zone"] = result["zone"]
    save_store(store)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jst = time.gmtime(now + 9 * 3600)
    updated_at_str = time.strftime("%Y-%m-%d %H:%M JST", jst)
    html = render_html(result, price, updated_at_str, MODES[mode_key]["label"])
    OUTPUT_HTML.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
