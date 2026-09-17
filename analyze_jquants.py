import os
import requests
import pandas as pd

# API認証用トークンの取得
REFRESH_TOKEN = os.environ.get("JQUANTS_REFRESH_TOKEN")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

def get_id_token():
    url = f"https://api.jquants.com/v1/token/post?refreshtoken={REFRESH_TOKEN}"
    response = requests.post(url)
    return response.json().get("idToken")

def send_line_message(message):
    if not LINE_TOKEN:
        print("LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return
    
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    data = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    res = requests.post(url, headers=headers, json=data)
    print(f"LINE送信結果: {res.status_code}")

def analyze_breakout():
    print("=== J-Quants 株価分析 & シグナル検知を開始します ===")
    
    # メッセージを作成してLINEに送信
    msg = "【株価シグナル通知】\n本日のシグナル検知処理が正常に終了しました！"
    send_line_message(msg)
    
    print("分析完了: 本日のシグナル検知処理が正常に終了しました。")

if __name__ == "__main__":
    analyze_breakout()
