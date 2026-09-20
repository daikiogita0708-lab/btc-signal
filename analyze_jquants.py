import os
import requests

def send_line_message(message):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    
    if not token or not user_id:
        print("エラー: LINEトークンかユーザーIDがありません")
        return
        
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }
    
    response = requests.post(url, headers=headers, json=data)
    print(f"LINE送信結果: {response.status_code} {response.text}")

if __name__ == "__main__":
    print("J-Quants APIからのデータ取得とモメンタム分析を開始します...")
    # --- ここに後でJ-Quantsの分析コードを入れます ---
    print("分析完了: 本日の処理が正常終了しました。")
    
    # LINEにテストメッセージを送信
    send_line_message("GitHub Actionsからテスト送信！届きましたか？")
