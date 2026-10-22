# -*- coding: utf-8 -*-
"""
daily_report.py — BTC 日次レポート（1日1回、LINEへ送信）
======================================================================
その時点の判定（買い時／静観／危険／収集中）・理由・金額の目安を
「毎日必ず」LINEに届けます。bot.py（買い時アラート）とは別の、
独立したワークフローから実行される想定です。

■ このスクリプトも実際の注文は一切送りません（アラート専用）。
"""

import os
import time

from bot import (
    BitflyerAPIError,
    BitflyerClient,
    DEFAULT_MODE,
    LOOKBACK_HOURS,
    MODES,
    PRODUCT_CODE,
    ZONE_STYLE,
    evaluate,
    fetch_balances,
    format_amount_section,
    format_balance_line,
    load_store,
    notify_all,
)


def build_report_message(result, price, jpy, btc, sample_count):
    price_text = f"{price:,.0f}円" if price is not None else "取得できませんでした"
    return (
        f"📊 本日のBTCレポート（{result['mode_label']}）\n"
        f"判定: {result['emoji']} {result['title']}\n"
        f"現在価格: {price_text}\n"
        f"理由: {result['reason']}\n"
        f"（判定に使った価格データ: 直近{LOOKBACK_HOURS:.0f}時間で{sample_count}件）\n\n"
        f"{format_amount_section(result, jpy)}"
        f"{format_balance_line(jpy, btc)}\n\n"
        f"※過去の平均との比較による目安で、儲かる保証はありません。自動購入もしません。買う／買わないはご自身で決めてください。"
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

    cutoff = now - LOOKBACK_HOURS * 3600
    history_tuples = [(t, p) for [t, p] in store.get("history", []) if t >= cutoff]

    if price is not None:
        result = evaluate(price, now, history_tuples, mode_key)
    else:
        style = ZONE_STYLE["error"]
        result = {
            "zone": "error", "reason": "価格を取得できませんでした。",
            "mode_label": MODES[mode_key]["label"], "deviation_pct": None,
            "baseline_avg": None, **style,
        }

    jpy, btc = fetch_balances(client)
    message = build_report_message(result, price, jpy, btc, len(history_tuples))

    # 日次レポートは判定にかかわらず「毎回必ず」送る
    sent = notify_all(message)
    print(f"[INFO] daily_report zone={result['zone']} samples={len(history_tuples)} sent={sent}")


if __name__ == "__main__":
    main()
