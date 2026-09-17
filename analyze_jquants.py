import os
import requests

# 環境変数からトークンを取得
JQUANTS_REFRESH_TOKEN = os.environ.get("JQUANTS_REFRESH_TOKEN")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

def send_line_message(message):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    payload = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    res = requests.post(url, headers=headers, json=payload)
    print(f"LINE送信結果: {res.status_code}")

def main():
    print("=== J-Quants 処理開始 ===")
    
    # テスト送信メッセージ
    msg = "【株価シグナル通知】\nJ-Quantsからのデータ取得テストです！LINE通知の設定が正常に完了しました。"
    
    if LINE_TOKEN:
        send_line_message(msg)
    else:
        print("LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")

if __name__ == "__main__":
    main()
- name: Run Analysis and Send LINE
        env:
          LINE_STOCK_TOKEN: ${{ secrets.LINE_STOCK_TOKEN }}
        run: python analyze_jquants.py
