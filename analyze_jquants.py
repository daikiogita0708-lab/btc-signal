import os
import requests

def send_line_message(message):
    token = os.environ.get("LINE_STOCK_TOKEN")
    if not token:
        print("エラー: LINE_STOCK_TOKEN が設定されていません。")
        return
    
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    data = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        print("LINEへの送信に成功しました！")
    else:
        print(f"送信失敗: {response.status_code} {response.text}")

def main():
    print("=== 株価データの分析を開始します ===")
    
    # テストメッセージをLINEに送信
    msg = "📈 株価分析botテストメッセージです！\n正常に通知が届きました。"
    send_line_message(msg)
    
    print("分析が正常に終了しました。")

if __name__ == "__main__":
    main()
