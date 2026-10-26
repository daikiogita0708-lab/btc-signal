# -*- coding: utf-8 -*-
"""
bot.py — ビットコイン「平均より安め」お知らせ（クラウド版 / GitHub Actions用）
======================================================================
15分ごとに価格をチェックし、直近の平均より安めの水準（一時的な下落）になったら
LINEに「平均より安め（目安）」とお知らせします。
（過去の検証では、このタイミングで買っても有利とは言えませんでした。あくまで目安です）

■ このbotは実際の注文は一切送りません（アラート専用）。
  買う/買わないの最終判断は、必ずご自身でbitFlyerアプリから行ってください。
  （自動購入コードは意図的に持たせていません。資金を勝手に動かしません）
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
DEFAULT_MODE = "aggressive"          # 意味のある攻めモード（押し目を敏感に狙う）
PRODUCT_CODE = "BTC_JPY"
NOTIFY_COOLDOWN_SECONDS = 60 * 60    # アラートの最短間隔（1時間）

# ---- 金額の目安（状況に合わせて変える）----
# 「円残高に対する割合」を、下落幅(押し目の深さ)とゾーンで変える。(控えめ, 標準, 攻め)
AMOUNT_LABELS = ("控えめ", "標準", "攻め")
AMOUNT_RATIOS = {
    "buy_small":  (0.05, 0.10, 0.20),   # 下落が-1%台（小さめの押し目）
    "buy_medium": (0.10, 0.20, 0.35),   # 下落が-2%台〜-4%手前
    "buy_large":  (0.15, 0.30, 0.50),   # 下落が-4%以上（大きな下落）
    "watch":      (0.02, 0.05, 0.10),   # 静観ゾーン（見送りが基本）
    "danger":     (0.01, 0.02, 0.03),   # 危険ゾーン（高値圏。見送り推奨）
}
# 円残高が取得できなかった時だけ使う固定額（円）
AMOUNT_FALLBACK_JPY = {
    "buy_small":  (1000, 2000, 3000),
    "buy_medium": (2000, 3000, 5000),
    "buy_large":  (3000, 5000, 8000),
    "watch":      (500, 1000, 2000),
    "danger":     (500, 1000, 1500),
}
TIER_TEXT = {
    "buy_small": "小さめの値下がり",
    "buy_medium": "中くらいの値下がり",
    "buy_large": "大きめの下落",
}
MAX_RATIO_OF_BALANCE = 0.5   # 1回の提案額は円残高の半分まで
ROUND_UNIT_JPY = 100         # 提案額は100円単位に丸める

# ---- 「平均より安め」アラートの出し方（通知を増やしすぎないためのルール）----
# 過去8年の検証（backtest.py）で、続報には追加の価値が見られず、通知の頻度も多めだったため、
# 続報を止め、1日の上限を 2通→1通 にしました。
MAX_ALERTS_PER_DAY = 1       # 1日(日本時間)に送るアラートの上限
FOLLOWUP_ENABLED = False     # 同じ下落局面での「続報」。False=送らない（True にすると以前の動きに戻る）
FOLLOWUP_DROP_PCT = 1.5      # 続報を送る条件（FOLLOWUP_ENABLED=True の時だけ使う）: 前回通知の価格から、さらにこの%以上下がった時

# 通知の末尾に添える、検証結果の一言（「買い時」と受け取られすぎないようにするため）
VERIFICATION_NOTE = (
    "※過去約8年の検証では、この目安で買っても、適当な日に買うより有利とは言えませんでした。"
    "通知のあと7日以内に3%以上さらに下がることも、約半分ありました。"
)

STORE_PATH = Path("state/store.json")
OUTPUT_DIR = Path("public")
OUTPUT_HTML = OUTPUT_DIR / "index.html"


# ============================================================
# シグナル判定ロジック（変更なし）
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
        "buy_threshold_pct": -1.0,  # 直近平均から-1%下がったら押し目と判定
        "danger_threshold_pct": 8.0,
    },
}

MIN_SAMPLES = 2
MIN_SPAN_HOURS = 1.0
LOOKBACK_HOURS = 24.0

ZONE_STYLE = {
    "buy": {"emoji": "🟢", "title": "平均より安め（目安）", "bg": "#2ecc71", "fg": "#0b3d20"},
    "watch": {"emoji": "🟡", "title": "静観ゾーン", "bg": "#f5c518", "fg": "#4a3800"},
    "danger": {"emoji": "🔴", "title": "危険ゾーン", "bg": "#e74c3c", "fg": "#ffffff"},
    "collecting": {"emoji": "⏳", "title": "データ収集中", "bg": "#95a5a6", "fg": "#ffffff"},
    "error": {"emoji": "⚠️", "title": "通信エラー", "bg": "#7f8c8d", "fg": "#ffffff"},
}


def evaluate(current_price, current_time, history, mode_key=DEFAULT_MODE):
    """現在価格を、過去の履歴の平均と比べて「平均より安め(buy)／静観(watch)／危険(danger)」を判定する。"""
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
            f"直近平均（{baseline_avg:,.0f}円）より{abs(deviation_pct):.1f}%安い水準です。"
        )
    elif deviation_pct >= mode["danger_threshold_pct"]:
        zone = "danger"
        reason = (
            f"直近平均（{baseline_avg:,.0f}円）より{deviation_pct:.1f}%高く、高値掴みのリスクがあるため見送りが無難です。"
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
# bitFlyer API（価格取得・残高確認のみ。発注機能は持たせません）
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
        """現在価格を取得（Public API・認証不要）。"""
        return self._request("GET", "/v1/ticker", params={"product_code": product_code})

    def get_balance(self):
        """残高を取得（Private API・認証必要）。通知に添える参考情報としてのみ使用。"""
        if not self.has_credentials():
            raise BitflyerAPIError("APIキーが設定されていません")
        return self._request("GET", "/v1/me/getbalance", private=True)

    # ※ 発注用メソッド（send_buy_order 等）は意図的に実装していません。
    #   このbotは資金を一切動かしません。

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
            raise BitflyerAPIError(f"通信エラー: {type(exc).__name__}")

        if resp.status_code == 429:
            raise BitflyerAPIError("レートリミットに達しました")
        if resp.status_code == 401:
            raise BitflyerAPIError("認証に失敗しました（APIキーを確認してください）")
        if not resp.ok:
            raise BitflyerAPIError(f"bitFlyerエラー（HTTP {resp.status_code}）")
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
    """通知を送る。戻り値は成功したかどうか（ログ出力用）。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        return False
    return send_line(message)


def fetch_balances(client):
    """(円残高, BTC残高) を返す。取れなかったら (None, None)。
    失敗しても通知は止めない。失敗理由の種類だけログに残す（金額・鍵は出さない）。"""
    if not client.has_credentials():
        print("[WARN] balance_skipped reason=no_credentials")
        return None, None
    try:
        balances = client.get_balance()
    except BitflyerAPIError as e:
        print(f"[WARN] balance_fetch_failed reason={e}")
        return None, None
    try:
        jpy = next((float(b["available"]) for b in balances if b.get("currency_code") == "JPY"), None)
        btc = next((float(b["available"]) for b in balances if b.get("currency_code") == "BTC"), None)
    except (AttributeError, TypeError, KeyError, ValueError):
        print("[WARN] balance_parse_failed")
        return None, None
    if jpy is None:
        print("[WARN] balance_parse_failed reason=no_jpy_entry")
    return jpy, btc


def format_balance_line(jpy, btc):
    if jpy is None:
        return ""
    btc_text = f" / {btc:.8f} BTC" if btc is not None else ""
    return f"\n残高目安: {jpy:,.0f}円{btc_text}"


def amount_tier(result):
    """判定結果から、金額の目安の区分を決める。判定不能なら None。"""
    zone = result.get("zone")
    dev = result.get("deviation_pct")
    if zone == "buy" and dev is not None:
        if dev <= -4.0:
            return "buy_large"
        if dev <= -2.0:
            return "buy_medium"
        return "buy_small"
    if zone in ("watch", "danger"):
        return zone
    return None


def suggest_amounts(result, balance_jpy=None):
    """状況（ゾーン・下落幅）と円残高に合わせて、金額の目安を最大3つ返す。
    戻り値: {"tier", "basis"("balance"|"fixed"|"no_balance"), "items":[(ラベル, 円, 割合or None)]} / 判定不能なら None"""
    tier = amount_tier(result)
    if tier is None:
        return None

    if balance_jpy is None:
        raw = [(lab, amt, None) for lab, amt in zip(AMOUNT_LABELS, AMOUNT_FALLBACK_JPY[tier])]
        basis = "fixed"
    elif balance_jpy < ROUND_UNIT_JPY:
        return {"tier": tier, "basis": "no_balance", "items": []}
    else:
        cap = int(balance_jpy * MAX_RATIO_OF_BALANCE // ROUND_UNIT_JPY) * ROUND_UNIT_JPY
        raw = []
        for lab, ratio in zip(AMOUNT_LABELS, AMOUNT_RATIOS[tier]):
            amt = int(balance_jpy * ratio / ROUND_UNIT_JPY + 0.5) * ROUND_UNIT_JPY
            amt = max(ROUND_UNIT_JPY, min(amt, cap)) if cap >= ROUND_UNIT_JPY else ROUND_UNIT_JPY
            if amt <= balance_jpy:
                raw.append((lab, amt, ratio))
        basis = "balance"

    items, seen = [], set()
    for lab, amt, ratio in raw:      # 同額が並んだら1つにまとめる
        if amt not in seen:
            seen.add(amt)
            items.append((lab, amt, ratio))
    return {"tier": tier, "basis": basis, "items": items}


def format_amount_section(result, balance_jpy=None):
    """通知に添える「金額の目安」ブロック（末尾に改行なし）。"""
    s = suggest_amounts(result, balance_jpy)
    if s is None:
        return "金額の目安: 判定できていないため、今回は提案なし"

    zone = result["zone"]
    if zone == "buy":
        dev = result.get("deviation_pct")
        head = f"買うなら金額の目安（ご自身でアプリから）\n  下落幅 {abs(dev):.1f}%＝{TIER_TEXT[s['tier']]}の想定"
    elif zone == "watch":
        head = "見送りが基本。それでも買うなら少額で"
    else:
        head = "高値圏なので見送り推奨。どうしても買うなら最少額で"

    if s["basis"] == "no_balance":
        return f"{head}\n・円の残高がほぼ無いため、提案できる額がありません"

    lines = []
    for lab, amt, ratio in s["items"]:
        pct = f"（残高の{ratio * 100:.0f}%）" if ratio is not None else ""
        lines.append(f"・{lab}: {amt:,}円{pct}")
    if s["basis"] == "balance":
        foot = "※円残高に対する割合で計算。1回の提案は残高の半分までに抑えています"
    else:
        foot = "※円残高が取れていないため固定の目安額です。実際の残高を超えないように"
    return head + "\n" + "\n".join(lines) + "\n" + foot


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
            data.setdefault("last_alert_price", None)   # 最後にアラートを送った時の価格
            data.setdefault("alert_day", None)          # 上限カウント用の日付(日本時間)
            data.setdefault("alerts_today", 0)          # その日に送ったアラート数
            return data
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return {"history": [], "last_zone": None, "last_notified_at": 0.0,
            "last_alert_price": None, "alert_day": None, "alerts_today": 0}


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
<title>ビットコイン価格チェック（攻めモード）</title>
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
  .note {{ max-width: 480px; margin: 10px auto 0; font-size: 11px; color: #aaa; text-align: center; }}
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
  <div class="note">※アラート専用です。自動購入は行いません。</div>
</body>
</html>
"""


# ============================================================
# 「平均より安め」アラートを送るかどうかの判定
# ============================================================
def jst_date_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + 9 * 3600))


def alerts_sent_today(store, now):
    """今日(日本時間)にすでに送ったアラート数。日付が変わっていれば0。"""
    return store.get("alerts_today", 0) if store.get("alert_day") == jst_date_str(now) else 0


def decide_alert(store, price, now):
    """「平均より安め(buy)」ゾーンのとき、アラートを送るか決める。
    戻り値: (送るか, 種別 "first"|"followup"|None, ログ用の理由)
      first    … 平均より安めのゾーンに入った最初の通知
      followup … （FOLLOWUP_ENABLED=True の時だけ）同じ下落局面で、前回通知からさらに
                 FOLLOWUP_DROP_PCT% 以上下がった時の続報
    FOLLOWUP_ENABLED=False（現在の設定）では、同じ局面が続いている間は、さらに下がっても送らない
    （理由 same_episode）。ゾーンをいったん離れて入り直した時は、あらためて「最初の通知」になる。
    """
    if alerts_sent_today(store, now) >= MAX_ALERTS_PER_DAY:
        return False, None, "daily_cap"
    if now - store.get("last_notified_at", 0.0) < NOTIFY_COOLDOWN_SECONDS:
        return False, None, "cooldown"
    if store.get("last_zone") != "buy":
        return True, "first", "new_episode"
    if not FOLLOWUP_ENABLED:
        return False, None, "same_episode"
    base = store.get("last_alert_price")
    if not base or base <= 0:
        return False, None, "no_baseline"
    drop_pct = (base - price) / base * 100.0
    if drop_pct >= FOLLOWUP_DROP_PCT:
        return True, "followup", "further_drop"
    return False, None, "no_further_drop"


def build_alert_message(result, price, kind, jpy, btc, nth, prev_price=None):
    if kind == "followup" and prev_price:
        drop = (prev_price - price) / prev_price * 100.0
        head = (
            f"🟢 平均より安め（目安）・続報｜{result['mode_label']}\n"
            f"前回の通知（{prev_price:,.0f}円）からさらに{drop:.1f}%下落しました。\n"
            f"※下落が続いている局面です。このあとさらに下がる可能性もあります。\n"
        )
    else:
        head = f"🟢 平均より安め（目安）｜{result['mode_label']}\n"
    return (
        f"{head}"
        f"現在価格: {price:,.0f}円\n"
        f"理由: {result['reason']}\n\n"
        f"{format_amount_section(result, jpy)}"
        f"{format_balance_line(jpy, btc)}\n\n"
        f"（本日{nth}通目／上限{MAX_ALERTS_PER_DAY}通）\n"
        f"{VERIFICATION_NOTE}\n"
        f"※「直近平均より安い」というだけの目安で、儲かる保証はありません。自動購入もしません。最終判断はお任せします。"
    )


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
        print(f"[WARN] price_fetch_failed type={type(e).__name__}")

    if price is not None:
        store["history"].append([now, price])

    cutoff = now - LOOKBACK_HOURS * 3600
    store["history"] = [[t, p] for [t, p] in store["history"] if t >= cutoff]

    if price is not None:
        history_tuples = [(t, p) for [t, p] in store["history"]]
        result = evaluate(price, now, history_tuples, mode_key)
        print(f"[INFO] price_ok zone={result['zone']} samples={len(history_tuples)}")
    else:
        style = ZONE_STYLE["error"]
        result = {
            "zone": "error", "reason": "価格を取得できませんでした。",
            "mode_label": MODES[mode_key]["label"], "deviation_pct": None, "baseline_avg": None, **style,
        }

    # 「平均より安め」ゾーンになった時だけアラート通知（発注は一切しない）
    alert_failed = False
    if result["zone"] == "buy":
        should_send, kind, why = decide_alert(store, price, now)
        if not should_send:
            print(f"[INFO] alert_skipped reason={why}")
            if why == "no_baseline":
                # （続報が有効な時だけ起こる）以前の状態(続報の基準価格なし)から引き継いだ場合は、今の価格を基準にする
                store["last_alert_price"] = price
        else:
            nth = alerts_sent_today(store, now) + 1
            prev_price = store.get("last_alert_price")
            jpy, btc = fetch_balances(client)
            message = build_alert_message(result, price, kind, jpy, btc, nth, prev_price)
            sent = notify_all(message)
            print(f"[INFO] alert kind={kind} nth_today={nth} sent={sent}")
            if sent:
                # 送れた時だけ記録する（送信に失敗した時は、上限を消費せず次回また試す）
                store["last_notified_at"] = now
                store["last_alert_price"] = price
                store["alert_day"] = jst_date_str(now)
                store["alerts_today"] = nth
            else:
                alert_failed = True

    # 送信に失敗した時は「平均より安めゾーンに入った」ことを記録しない（次回また最初の通知として試す）
    if not alert_failed:
        store["last_zone"] = result["zone"]
    save_store(store)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jst = time.gmtime(now + 9 * 3600)
    updated_at_str = time.strftime("%Y-%m-%d %H:%M JST", jst)
    html = render_html(result, price, updated_at_str, MODES[mode_key]["label"])
    OUTPUT_HTML.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
