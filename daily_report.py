# -*- coding: utf-8 -*-
"""
daily_report.py — BTC 日次レポート（1日1回、LINEへ送信）
======================================================================
その時点の判定（買い時／静観／危険／収集中）・理由・金額の目安パターンを
「毎日必ず」LINEに届けます。bot.py（15分ごとの買い時アラート）とは別の、
独立したワークフローから実行される想定です。

■ このスクリプトも実際の注文は一切送りません（アラート専用）。
"""

import os
import time

from bot import (
    BitflyerAPIError,
    BitflyerClient,
    DEFAULT_MODE,
    MODES,
    PRODUCT_CODE,
    ZONE_STYLE,
    evaluate,
    fetch_balance_line,
    format_amount_patterns,
    load_store,
    notify_all,
)


def build_report_message(result, price, balance_line=""):
    price_text = f"{price:,.0f}円" if price is not None else "取得できませんでした"
    return (
        f"📊 本日のBTCレポート（{result['mode_label']}）\n"
        f"判定: {result['emoji']} {result['title']}\n"
        f"現在価格: {price_text}\n"
        f"理由: {result['reason']}\n\n"
        f"買うなら金額の目安（ご自身でアプリから）:\n"
        f"{format_amount_patterns()}"
        f"{balance_line}\n\n"
        f"※これは判断材料です。買う／買わないはご自身で決めてください。自動購入はしません。"
    )


def main():
    mode_key = DEFAULT_MODE
    client = BitflyerClient(
        api_key=os.environ.get("BITFLYER_API_KEY"),
        api_secret=os.environ.get("BITFLYER_API_SECRET"),
    )

    store = load_store()
    now = time.time()

    price = None
    try:
        ticker = client.get_ticker(PRODUCT_CODE)
        price = float(ticker["ltp"])
    except (BitflyerAPIError, KeyError, ValueError, TypeError) as e:
        print(f"[WARN] price_fetch_failed type={type(e).__name__}")

    history_tuples = [(t, p) for [t, p] in store.get("history", [])]

    if price is not None:
        result = evaluate(price, now, history_tuples, mode_key)
    else:
        style = ZONE_STYLE["error"]
        result = {
            "zone": "error", "reason": "価格を取得できませんでした。",
            "mode_label": MODES[mode_key]["label"], "deviation_pct": None,
            "baseline_avg": None, **style,
        }

    balance_line = fetch_balance_line(client)
    message = build_report_message(result, price, balance_line)

    # 日次レポートは判定にかかわらず「毎回必ず」送る
    sent = notify_all(message)
    print(f"[INFO] daily_report zone={result['zone']} samples={len(history_tuples)} sent={sent}")


if __name__ == "__main__":
    main()
