"""
indicators.py
-------------
fetch_data.py が保存したCSV(例: data_72030.csv)を読み込み、
テクニカル指標と「攻めのシグナル」を追加する。

■ 実行方法
    $ python indicators.py data_72030.csv

■ 列名について
    J-Quantsのレスポンス列名は環境によって確認が必要なため、
    下の COLUMN_MAP を実際のCSVの列名(fetch_data.py実行時に表示された
    df.tail() で確認できるはず)に合わせて調整すること。
"""

import sys

import pandas as pd

# 実際のCSVの列名がこれと違う場合はここだけ書き換えればいい
COLUMN_MAP = {
    "date": "Date",
    "close": "Close",
    "volume": "Volume",
}


# ============ テクニカル指標 ============

def add_moving_averages(df: pd.DataFrame, close_col: str, windows=(5, 25, 75)) -> pd.DataFrame:
    """単純移動平均線を追加する。5・25・75日は日本株の定番の組み合わせ。"""
    for w in windows:
        df[f"MA{w}"] = df[close_col].rolling(window=w).mean()
    return df


def add_rsi(df: pd.DataFrame, close_col: str, period: int = 14) -> pd.DataFrame:
    """RSI(相対力指数)。ワイルダー式の平滑化(EWM, alpha=1/period)で計算。"""
    delta = df[close_col].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    df[f"RSI{period}"] = 100 - (100 / (1 + rs))
    return df


def add_daily_change(df: pd.DataFrame, close_col: str) -> pd.DataFrame:
    """前日比を円ベース・%ベースの両方で追加する。"""
    df["change"] = df[close_col].diff()
    df["change_pct"] = df[close_col].pct_change() * 100
    return df


# ============ 攻めのシグナル判定 ============

def add_volume_spike_signal(
    df: pd.DataFrame, volume_col: str, window: int = 20, threshold: float = 2.0
) -> pd.DataFrame:
    """
    出来高が「過去window日平均のthreshold倍以上」ならTrue。
    当日の出来高を平均の計算に混ぜると基準を自分で吊り上げてしまうため、
    shift(1)で「前日までの平均」を基準にしている。
    """
    avg_volume = df[volume_col].shift(1).rolling(window=window).mean()
    df[f"volume_avg{window}"] = avg_volume
    df["volume_spike"] = df[volume_col] >= avg_volume * threshold
    return df


def add_golden_cross_signal(df: pd.DataFrame, short_col: str, long_col: str) -> pd.DataFrame:
    """
    ゴールデンクロス: 短期線が長期線を「下から上に抜けた瞬間」だけTrue。
    (短期線>長期線が続く間ずっとTrueにすると、発生日を特定できないため)
    """
    is_above = df[short_col] > df[long_col]
    was_below = df[short_col].shift(1) <= df[long_col].shift(1)
    df[f"golden_cross_{short_col}_{long_col}"] = is_above & was_below
    return df


def main():
    if len(sys.argv) < 2:
        sys.exit("使い方: python indicators.py <fetch_data.pyが出力したCSVファイル>")

    input_path = sys.argv[1]
    df = pd.read_csv(input_path)

    missing = [c for c in COLUMN_MAP.values() if c not in df.columns]
    if missing:
        sys.exit(
            f"想定した列が見つかりません: {missing}\n"
            f"実際の列名: {list(df.columns)}\n"
            f"→ ファイル冒頭の COLUMN_MAP を実際の列名に合わせて書き換えてください。"
        )

    date_col = COLUMN_MAP["date"]
    close_col = COLUMN_MAP["close"]
    volume_col = COLUMN_MAP["volume"]

    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)  # 日付昇順は必須(移動平均・前日比の前提)

    df = add_moving_averages(df, close_col)
    df = add_rsi(df, close_col)
    df = add_daily_change(df, close_col)
    df = add_volume_spike_signal(df, volume_col)
    df = add_golden_cross_signal(df, "MA5", "MA25")   # 短期(攻めの初動狙い)
    df = add_golden_cross_signal(df, "MA25", "MA75")  # 中期(伝統的なゴールデンクロス)

    output_path = input_path.replace(".csv", "_signals.csv")
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"完了: {output_path} に保存しました。")

    any_signal = df["volume_spike"] | df["golden_cross_MA5_MA25"] | df["golden_cross_MA25_MA75"]
    hits = df[any_signal]
    if hits.empty:
        print("この期間ではシグナルは検出されませんでした。")
    else:
        print(f"\nシグナル検出: {len(hits)}件")
        cols = [date_col, close_col, "RSI14", "volume_spike", "golden_cross_MA5_MA25", "golden_cross_MA25_MA75"]
        print(hits[cols].tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
