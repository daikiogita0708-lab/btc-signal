"""
analyze_jquants.py  (v5)
-------------------------
GitHub Actions(jquants_fetch.yml、平日朝の自動実行)から動く、
日本株のテクニカル分析デイリーレポートをLINEに届けるスクリプト。

■ v5での変更点
    1. 市場スキャンを追加(market_scan.py)。監視10銘柄だけでなく、東証プライム全体から
       「3ヶ月で強い株」「売買代金が急増した株」「値上がり/値下がり率上位」を毎日拾い、
       上位候補はテクニカルで深掘りする → 毎日ちがう銘柄に出会える
    2. 「もし買うなら」の資金管理を自動計算。授業料予算と1回の損失上限から、
       損切りラインと最大株数を出す(=失敗しても致命傷にならない買い方)
    3. 株専用のLINEチャネルに対応。Secretsに LINE_STOCK_CHANNEL_ACCESS_TOKEN を登録すると
       BTCのLINEではなく株専用のLINEから届く(未登録なら今まで通りBTCのLINEから)
    4. 吹き出しを5つに(サマリー / 市場スキャン / 仮想運用 / 銘柄別データ / 今日の学び)

■ v4での変更点
    1. 仮想運用(ペーパートレード)を追加(paper_trading.py)。
       「順張り」「逆張り」「均等に買って放置」の3つのルールで、仮想の100万円を
       実際に運用していたらどうなっていたかを毎日計算し、今日の仮想売買とあわせて届ける。
       実際に個別株を買い始める前に、ルールの実力をタダで確かめるための"練習試合"。
    2. 今日の用語を25語→42語に拡充(資金管理・NISA・注文方法など、実際に買う前に必要な知識)
    3. LINEの吹き出しを4つに(サマリー / 仮想運用 / 銘柄別データ / 今日の学び)

■ v3での変更点
    1. 毎日中身が変わるレポートに作り替えた
       - 「今日の変化(前日から何が起きたか)」を検出して表示
       - 全銘柄を「テクニカル強弱スコア」で順位付けし、毎日ランキングを表示
       - 用語解説は毎回同じ3つを載せるのをやめ、約25語の中から毎日1語を
         ローテーションで深掘り。しかも「今日のデータだとこの銘柄が当てはまる」
         という実例つき
    2. 指標を増やした: 移動平均乖離率、1年レンジ内の位置、出来高倍率、
       平均変動幅(ボラティリティ)、トレンド判定
    3. 過去実績(勝率)・相場地合い(ブレッド)を統合
    4. LINEの吹き出しを3つに分割(サマリー / 銘柄別データ / 今日の学び)

■ 判定しているシグナル
    買いチャンス候補: RSI 30以下(売られすぎ) または ゴールデンクロス(MA25×MA75)
    過熱・注意:       RSI 70以上(買われすぎ) または 25日線乖離率 +10%以上

■ 正直な注意点
    - RSIや移動平均線は世界中の投資家が知っている一般的な指標で、これだけで
      継続的に市場に勝てる保証はない。目的は「相場の見方を体で覚えること」と
      「思いつきで飛びつく前に根拠を確認する習慣をつけること」。
    - 「過去実績」は同じ銘柄の過去データでの機械的な集計。N(件数)が少ない
      統計は偶然の可能性が高く、将来の再現性も保証されない。投資助言ではない。
    - Freeプランはデータが約12週間遅れ。有料プランに切り替えたら、
      環境変数 JQUANTS_DELAY_WEEKS=0 にすれば遅延表示も自動で消える。

■ このファイルを動かす前提
    fetch_data.py と indicators.py が同じフォルダ(リポジトリのルート直下)にあること。
    実行時間の目安: Freeプランでは10銘柄×約13秒 = 約2〜3分(レート制限のため)。

■ 環境変数
    JQUANTS_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
    (任意) JQUANTS_DELAY_WEEKS, JQUANTS_MIN_INTERVAL_SEC
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

import fetch_data
import indicators
import market_scan
import paper_trading

# ============ 監視銘柄(主要10銘柄) ============
WATCHLIST = {
    "94320": "NTT",
    "72030": "トヨタ自動車",
    "67580": "ソニーグループ",
    "99840": "ソフトバンクグループ",
    "83060": "三菱UFJフィナンシャルG",
    "79740": "任天堂",
    "68610": "キーエンス",
    "99830": "ファーストリテイリング",
    "40630": "信越化学工業",
    "65010": "日立製作所",
}

# ============ 判定の閾値 ============
RSI_OVERSOLD_TH = 30
RSI_OVERBOUGHT_TH = 70
DEV_HOT_TH = 10.0          # 25日線乖離率がこれ以上: 過熱
VOLUME_SPIKE_RATIO = 2.0   # 出来高が20日平均のこの倍率以上: 急増
NEW_HIGH_LOW_WINDOW = 60   # 何日高値/安値を「更新」とみなすか
RANGE_WINDOW = 250         # 約1年(52週)のレンジ

# ============ 過去実績・地合い ============
# Freeプランで取れる期間は「12週間前〜2年12週間前」(公式仕様)。範囲外を指定するとAPIが400を返すので、
# 2年(730日)ぴったりではなく、日付のずれ(UTCとJST・うるう年)に備えて30日の余裕を持たせた700日にしている。
# 有料プランで長く取りたくなったら、この数字を増やす(Lightは5年分なので1200日くらいまでOK)。
LOOKBACK_CALENDAR_DAYS = 700   # 約2年弱を取得(過去実績の母数と1年レンジ計算のため)
FORWARD_RETURN_HORIZON = 5     # シグナルから何営業日後のリターンを見るか(約1週間)
MIN_RELIABLE_SAMPLE = 5        # これ未満のNは「参考程度」と注記
BREADTH_STRONG_TH = 60         # 上昇トレンド銘柄の比率(%)がこれ以上: 地合い強い
BREADTH_WEAK_TH = 40           # これ未満: 地合い弱い

# ============ 資金管理(「もし買うなら」の計算に使う) ============
# GitHubの Settings > Secrets and variables > Actions > Variables で自分の値に変えられる
TUITION_BUDGET_YEN = int(os.environ.get("TUITION_BUDGET_YEN") or 100_000)     # 授業料予算(全部失ってもいい額)
MAX_LOSS_PCT_PER_TRADE = float(os.environ.get("MAX_LOSS_PCT_PER_TRADE") or 5)  # 1回の取引で失ってよい割合(予算の%)

# ============ LINE ============
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
MAX_BUBBLE_LEN = 4800          # 吹き出し1つあたりの上限(5000文字)に対する安全マージン
JST = timezone(timedelta(hours=9))
WEEKDAY_JA = "月火水木金土日"
SEP = "\n" + "-" * 18 + "\n"
LAST_FETCH_ERROR = ""   # 直近のデータ取得エラー(全銘柄が失敗した時のLINEお知らせに使う)


# =====================================================================
# 今日の用語(ローテーション)
# 各要素: (用語, 解説, 今日のデータから実例を作る関数 or None)
# =====================================================================

def _top(results, key, reverse=True):
    valid = [r for r in results if r.get(key) is not None and not pd.isna(r.get(key))]
    return sorted(valid, key=lambda r: r[key], reverse=reverse)


def _ex_rsi(rs, paper=None):
    hi, lo = _top(rs, "rsi")[0], _top(rs, "rsi", reverse=False)[0]
    return f"RSIが最も高いのは{hi['name']}({hi['rsi']:.0f})、最も低いのは{lo['name']}({lo['rsi']:.0f})"


def _ex_dev(rs, paper=None):
    hi, lo = _top(rs, "dev25")[0], _top(rs, "dev25", reverse=False)[0]
    return f"25日線から最も上に離れているのは{hi['name']}({hi['dev25']:+.1f}%)、最も下は{lo['name']}({lo['dev25']:+.1f}%)"


def _ex_range(rs, paper=None):
    hi, lo = _top(rs, "range_pos")[0], _top(rs, "range_pos", reverse=False)[0]
    return f"1年の値幅の中で最も高値圏にいるのは{hi['name']}({hi['range_pos']:.0f}%)、最も安値圏は{lo['name']}({lo['range_pos']:.0f}%)"


def _ex_volume(rs, paper=None):
    hi = _top(rs, "volume_ratio")[0]
    return f"出来高が普段と比べて最も多いのは{hi['name']}(20日平均の{hi['volume_ratio']:.1f}倍)"


def _ex_vol(rs, paper=None):
    hi, lo = _top(rs, "vol20")[0], _top(rs, "vol20", reverse=False)[0]
    return f"値動きが最も荒いのは{hi['name']}(1日平均{hi['vol20']:.1f}%)、最も穏やかなのは{lo['name']}({lo['vol20']:.1f}%)"


def _ex_trend(rs, paper=None):
    counts = {k: sum(1 for r in rs if r["trend"] == k) for k in ("上昇", "もみ合い", "下降")}
    return f"今日は上昇トレンド{counts['上昇']}銘柄・もみ合い{counts['もみ合い']}銘柄・下降トレンド{counts['下降']}銘柄"


def _ex_cross(rs, paper=None):
    up = [r["name"] for r in rs if r["trend_up"]]
    return f"25日線が75日線より上にある(GC後の状態の)銘柄は{len(up)}/{len(rs)}銘柄"


def _ex_score(rs, paper=None):
    hi, lo = _top(rs, "score")[0], _top(rs, "score", reverse=False)[0]
    return f"スコア最高は{hi['name']}({hi['score']})、最低は{lo['name']}({lo['score']})"


def _ex_mdd(rs, paper=None):
    parts = [f"{s['name']}は最大{s['max_drawdown_pct']:.1f}%" for s in paper["strategies"]]
    return "仮想運用の最大ドローダウン: " + " / ".join(parts)


def _ex_benchmark(rs, paper=None):
    bh = paper["strategies"][-1]
    others = [f"{s['name']}{s['total_return_pct']:+.1f}%" for s in paper["strategies"][:-1]]
    return f"比較対象の「均等に買って放置」は{bh['total_return_pct']:+.1f}%。これに勝てているか: " + " / ".join(others)


def _ex_winrate(rs, paper=None):
    parts = []
    for s in paper["strategies"][:-1]:
        wr = "取引なし" if s["win_rate_pct"] is None else f"勝率{s['win_rate_pct']:.0f}%(取引{s['n_trades']}回)"
        parts.append(f"{s['name']}: {wr} → 総リターン{s['total_return_pct']:+.1f}%")
    return " / ".join(parts)


GLOSSARY_POOL = [
    ("RSI(相対力指数)",
     "直近14日間の値上がり幅と値下がり幅の比率から、0〜100で「買われすぎ・売られすぎ」を測る指標。"
     "70以上は買われすぎ、30以下は売られすぎの目安。ただし強い上昇相場ではRSI70超えが長く続くこともあり、"
     "「70を超えた=すぐ下がる」とは限らない点に注意。", _ex_rsi),
    ("移動平均線",
     "過去N日間の終値の平均を線でつないだもの。25日線は約1ヶ月、75日線は約3ヶ月の平均的な買いコストを表す。"
     "株価が線より上なら「平均的な買い手が含み益」の状態で、上昇の勢いがあると読む。", _ex_trend),
    ("ゴールデンクロス/デッドクロス",
     "短期線(25日)が長期線(75日)を下から上に抜けるのがゴールデンクロス、その逆がデッドクロス。"
     "トレンド転換のサインとされるが、移動平均は過去の平均なので「転換が起きた後」に遅れて出る。"
     "もみ合い相場ではクロスが頻発してダマシになりやすい。", _ex_cross),
    ("移動平均乖離率",
     "株価が移動平均線からどれだけ離れているかを%で表したもの。(株価-25日線)÷25日線。"
     "大型株では±10%前後を超えると「行き過ぎ」とされ、平均に戻ろうとする動きが出やすい。"
     "RSIと並ぶ日本で人気の逆張り指標。", _ex_dev),
    ("52週高値・安値(1年レンジ)",
     "過去1年の最高値と最安値の間で、今の株価がどの位置にいるかを0〜100%で表した。"
     "100%近く=1年の高値圏(強いが高値掴みリスクも)、0%近く=安値圏(割安かもしれないが下落が続く途中かも)。",
     _ex_range),
    ("出来高",
     "その日に売買が成立した株数。株価が上がっても出来高が少なければ「参加者が少ない上昇」で信頼度が低い。"
     "逆に出来高を伴う上昇・下落は、多くの投資家がそちらに動いた本気の動きと読む。", _ex_volume),
    ("ボラティリティ(変動率)",
     "値動きの激しさ。ここでは直近20日の1日あたり平均変動幅(%)で表示している。"
     "高い銘柄は大きく儲かる可能性も、大きく損する可能性もある。損切り幅を決める時の基準になる。", _ex_vol),
    ("トレンド(上昇・下降・もみ合い)",
     "株価>25日線>75日線の並びなら上昇トレンド、逆なら下降トレンド、それ以外はもみ合いと判定している。"
     "「トレンドは続く」というのが順張りの基本の考え方。", _ex_trend),
    ("テクニカル強弱スコア",
     "このレポート独自の指標。トレンドの向き・RSI・1年レンジ内の位置の3つを0〜100で平均した「今の勢い」の目安。"
     "高い=強い流れ(順張り候補だが過熱にも注意)、低い=弱い流れ(反発候補にもなるが下落途中かも)。"
     "売買の推奨ではなく、今の状態の要約。", _ex_score),
    ("相場地合い(ブレッド)",
     "市場全体のうち何%の銘柄が上昇トレンドにあるか。個別株にいいシグナルが出ても、"
     "地合いが弱い(多くの銘柄が下げている)時は逆張りが失敗しやすい。プロも必ず確認する視点。", None),
    ("順張りと逆張り",
     "順張り=上がっている株を買う(トレンドに乗る)。逆張り=下がった株を買う(反発を狙う)。"
     "ゴールデンクロスは順張り、RSI30以下は逆張りのシグナル。どちらが正しいではなく、相場の状況で向き不向きがある。",
     None),
    ("ダマシ",
     "シグナルが出たのに、その通りに値動きしなかったケース。ゴールデンクロスの直後に下落する、など。"
     "どんな指標にもダマシはあるので、1つの指標だけで判断せず、出来高や地合いと組み合わせて確認する。", None),
    ("損切り(ストップロス)",
     "買った株が想定と逆に動いた時、損失が小さいうちに売って確定させること。"
     "「買う前に、いくら下がったら売るかを決めておく」のが基本。ボラティリティが高い銘柄ほど損切り幅は広めに取る必要がある。",
     _ex_vol),
    ("利益確定",
     "含み益が出ている株を売って利益を確定させること。RSI70超えや乖離率+10%超えは、"
     "利益確定の売りが出やすいタイミングの目安とされる。", None),
    ("分散投資",
     "資金を複数の銘柄・業種・時期に分けて投資すること。1銘柄の暴落で資産全体が大きく傷つくのを防ぐ。"
     "同じ業種の銘柄ばかり買うと、分散しているつもりでも一緒に下がる点に注意。", None),
    ("ナンピン買い",
     "買った株が下がった時に買い増して、平均取得単価を下げること。反発すれば有利だが、"
     "下落トレンドが続くと損失が雪だるま式に膨らむ。初心者が最も失敗しやすい手法の一つと言われる。", None),
    ("指値注文と成行注文",
     "指値=「この値段なら買う/売る」と価格を指定する注文。成行=価格を指定せず、今の値段ですぐ売買する注文。"
     "成行は確実に約定するが、値動きが激しい時は想定より不利な価格になることがある。", None),
    ("PER(株価収益率)",
     "株価÷1株あたり利益。「利益の何年分の値段がついているか」を表し、低いほど割安とされる。"
     "ただし業種によって平均が大きく違うので、同じ業種の中で比べるのが基本。", None),
    ("PBR(株価純資産倍率)",
     "株価÷1株あたり純資産。1倍を下回ると「会社を解散して資産を分けた方が得」という理論上の割安状態。"
     "東証は近年、PBR1倍割れの企業に改善を求めている。", None),
    ("配当利回り",
     "1株あたりの年間配当÷株価。株価が下がると利回りは上がるので、「利回りが高い=良い株」とは限らない"
     "(業績悪化で株価が下がっているだけのことも)。", None),
    ("押し目買い",
     "上昇トレンドの途中で一時的に下がったところを買うこと。「トレンドは上向きのまま」が前提なので、"
     "25日線や75日線が上向きかどうかを確認するのが大事。", _ex_trend),
    ("高値掴み",
     "株価が大きく上がった後、天井付近で買ってしまうこと。RSIが高い・乖離率が大きい・1年レンジの上限付近、"
     "が重なっている時は特に注意。", _ex_range),
    ("バックテスト",
     "過去のデータに売買ルールを当てはめて、そのルールでどれくらい儲かったか(損したか)を検証すること。"
     "このレポートの「過去実績」は簡易的なバックテスト。", None),
    ("サンプル数(N)と統計の信頼性",
     "「勝率100%」でもN=2なら、たまたま2回とも上がっただけかもしれない。"
     "一般にNが30を超えると統計として意味を持ち始めると言われる。このレポートでN<5に注記をつけているのはそのため。",
     None),
    ("勝率と期待値",
     "勝率が高くても、負けた時の損が大きければトータルでは損をする。大事なのは"
     "「平均してどれくらい儲かるか(期待値)」。このレポートの過去実績で、勝率と平均リターンを両方載せているのはそのため。",
     _ex_winrate),
    ("仮想運用(ペーパートレード)",
     "実際のお金を使わず、ルール通りに売買したことにして成績を記録すること。"
     "「勝てると思ったルール」が本当に勝てるかを、損をせずに確かめられる。プロも新しい手法は必ずまずこれで試す。", _ex_benchmark),
    ("ベンチマーク",
     "自分の成績を比べる「ものさし」。個別株の売買を頑張っても、ただ均等に買って放置した場合や"
     "インデックスファンドに負けているなら、手間をかける意味がない。勝ったか負けたかは必ずベンチマークと比べて判断する。",
     _ex_benchmark),
    ("最大ドローダウン",
     "資産がピークから最大で何%減ったか。総リターンが同じでも、途中で-30%を経験する手法は精神的に続けられないことが多い。"
     "「どれだけ儲かるか」と同じくらい「どれだけ耐えられるか」が大事。", _ex_mdd),
    ("ポジションサイジング(資金配分)",
     "1銘柄にいくら投じるかを決めること。このレポートの仮想運用は「資金を3等分して最大3銘柄」。"
     "1銘柄に全力だと、1回の失敗で資産が大きく傷つく。プロは銘柄選びより資金配分を重視するとも言われる。", None),
    ("モメンタム効果",
     "「最近強い株は、しばらく強いまま」という傾向。学術研究でも多くの市場で確認されている有名な現象で、"
     "仮想運用の「順張り」はこれを狙ったもの。ただし日本株ではこの効果が弱いという研究もあり、"
     "相場の急な反転局面では大きく負けやすい弱点もある。本当に効くかは仮想運用の成績で確かめよう。", _ex_benchmark),
    ("リバランス(入れ替え)",
     "保有銘柄を定期的に見直して入れ替えること。仮想運用の順張りは毎週はじめに行う。"
     "頻繁すぎるとコストと手間が増え、少なすぎると弱った銘柄を持ち続けることになる。", None),
    ("単元株と単元未満株",
     "日本株は通常100株単位(単元株)でしか買えないので、株価5,000円なら最低50万円必要。"
     "SBI証券のS株や楽天証券のかぶミニなどの単元未満株なら1株から買える。ただし注文方法が限られることが多い"
     "(成行のみ・約定は1日数回など。会社ごとに違う)。", None),
    ("寄付(よりつき)と引け",
     "寄付はその日最初についた値段(始値)、引けは最後の値段(終値)。"
     "このレポートは終値で判断し、翌日の寄付で売買する想定で仮想運用している(=単元未満株の寄付取引と同じ形)。", None),
    ("逆指値注文",
     "「この値段まで下がったら売る」と事前に出しておく注文。損切りを自動化できる。"
     "単元株なら多くの証券会社で使えるが、単元未満株では使えないことが多いので、その場合は自分で売る必要がある。", None),
    ("スリッページ",
     "注文した時に想定していた価格と、実際に約定した価格のズレ。成行注文や値動きの激しい時に大きくなる。"
     "仮想運用でも片道0.1%のズレを見込んで、成績を甘く見積もらないようにしている。", None),
    ("コア・サテライト戦略",
     "資産の大部分(コア)は低コストのインデックスファンドで長期の積立、一部(サテライト)で個別株などに挑戦する考え方。"
     "サテライトで失敗しても資産全体は大きく傷つかない。個別株は「サテライト」の位置づけで始めるのが定石。", None),
    ("NISAのつみたて投資枠と成長投資枠",
     "新NISAは、つみたて投資枠(年120万円・投資信託の積立)と成長投資枠(年240万円・個別株も買える)の2つ。"
     "生涯で合計1,800万円(うち成長投資枠は1,200万円まで)まで、利益に税金がかからない。", None),
    ("NISAの損益通算と非課税枠",
     "NISA口座の損失は、他の口座の利益と相殺(損益通算)できない。また売った分の枠は翌年まで戻らない(2026年改正で前倒し予定)。"
     "損切りを前提に何度も売買する手法は、NISAより特定口座の方が向いている場合がある。", None),
    ("特定口座(源泉徴収あり)",
     "証券会社が税金の計算と納付を代わりにやってくれる口座。利益には約20%の税金がかかるが、"
     "損失が出た年は他の利益と相殺でき、確定申告も原則不要。", None),
    ("インデックス投資",
     "日経平均やS&P500などの指数にそのまま連動するファンドを買う方法。銘柄選びをしないので手間がかからず、"
     "長期では個別株を選ぶプロの多くがこれに勝てていないというデータもある。個別株で勝てているかを測るものさしにもなる。",
     _ex_benchmark),
    ("決算発表と株価",
     "企業は年4回業績を発表し、その内容と市場の予想との差で株価が大きく動く。"
     "テクニカル分析では予測できない動きなので、保有銘柄の決算日は事前に確認しておくのが基本。", None),
    ("信用取引",
     "お金や株を証券会社から借りて、手持ち資金の約3.3倍まで取引できる仕組み。利益も損失も膨らみ、"
     "元手以上の損失(追証)が出ることもある。仮想運用で安定して勝てるようになるまでは使わないのが無難。", None),
]


# =====================================================================
# LINE
# =====================================================================

def get_line_credentials() -> tuple[str, str]:
    """株専用チャネルのトークンがあればそれを使い、なければBTCと共用のトークンを使う。"""
    token = os.environ.get("LINE_STOCK_CHANNEL_ACCESS_TOKEN") or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_STOCK_USER_ID") or os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        sys.exit("エラー: LINEのチャネルアクセストークン または ユーザーID が設定されていません。")
    channel = "株専用チャネル" if os.environ.get("LINE_STOCK_CHANNEL_ACCESS_TOKEN") else "BTCと共用のチャネル"
    print(f"[INFO] LINE送信先: {channel}")
    return token, user_id


def send_line_messages(texts: list, token: str, user_id: str) -> bool:
    """最大5つの吹き出しを1回のPushでまとめて送る。"""
    bubbles = []
    for t in texts[:5]:
        if len(t) > MAX_BUBBLE_LEN:
            t = t[:MAX_BUBBLE_LEN] + "\n…(以下省略)"
        bubbles.append({"type": "text", "text": t})
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"to": user_id, "messages": bubbles}
    resp = requests.post(LINE_PUSH_URL, headers=headers, json=data, timeout=10)
    print(f"[DEBUG] LINE送信結果: {resp.status_code} {resp.text[:200]}")
    return resp.status_code == 200


# =====================================================================
# 分析
# =====================================================================

def compute_forward_return_stats(df: pd.DataFrame, signal_series: pd.Series, horizon: int,
                                 close_col: str = "Close") -> dict | None:
    """
    signal_seriesがTrueだった過去の各日について、horizon営業日後のリターン(%)を集計する。
    直近(まだhorizon日分の未来データがない日=今日など)は自動で除外されるので先読みにならない。
    """
    signal_idx = np.where(signal_series.fillna(False).to_numpy(dtype=bool))[0]
    valid_idx = signal_idx[signal_idx + horizon < len(df)]
    if len(valid_idx) == 0:
        return None
    close = df[close_col].to_numpy()
    returns_pct = (close[valid_idx + horizon] - close[valid_idx]) / close[valid_idx] * 100
    return {
        "n": int(len(returns_pct)),
        "avg_return_pct": float(np.mean(returns_pct)),
        "win_rate_pct": float((returns_pct > 0).mean() * 100),
    }


def format_stat(stats: dict | None) -> str:
    if stats is None:
        return "過去実績: データ不足"
    note = " ※件数少・参考程度" if stats["n"] < MIN_RELIABLE_SAMPLE else ""
    return (f"過去実績: {FORWARD_RETURN_HORIZON}営業日後 平均{stats['avg_return_pct']:+.1f}% "
            f"/ 勝率{stats['win_rate_pct']:.0f}% (N={stats['n']}){note}")


def detect_events(df: pd.DataFrame) -> list:
    """最新日と前日を比べて、『今日起きた変化』を文章で返す。"""
    if len(df) < NEW_HIGH_LOW_WINDOW + 2:
        return []
    cur, prev = df.iloc[-1], df.iloc[-2]
    events = []

    if cur["dead_cross_MA25_MA75"]:
        events.append("🔴デッドクロス発生(中期トレンドが下向きに転換)")
    if prev["Close"] <= prev["MA25"] and cur["Close"] > cur["MA25"]:
        events.append("株価が25日線を上抜け(短期の流れが上向きに)")
    if prev["Close"] >= prev["MA25"] and cur["Close"] < cur["MA25"]:
        events.append("株価が25日線を下抜け(短期の流れが下向きに)")
    if prev["RSI14"] <= 50 < cur["RSI14"]:
        events.append(f"RSIが50を上抜け({prev['RSI14']:.0f}→{cur['RSI14']:.0f}、勢いが上向きに)")
    if prev["RSI14"] >= 50 > cur["RSI14"]:
        events.append(f"RSIが50を下抜け({prev['RSI14']:.0f}→{cur['RSI14']:.0f}、勢いが下向きに)")
    if prev["RSI14"] > RSI_OVERSOLD_TH >= cur["RSI14"]:
        events.append("RSIが30以下に突入(売られすぎゾーン入り)")
    if prev["RSI14"] <= RSI_OVERSOLD_TH < cur["RSI14"]:
        events.append("RSIが30を上抜けて売られすぎゾーンを脱出(反発の初動の可能性)")
    if prev["RSI14"] < RSI_OVERBOUGHT_TH <= cur["RSI14"]:
        events.append("RSIが70以上に突入(買われすぎゾーン入り)")
    if prev["RSI14"] >= RSI_OVERBOUGHT_TH > cur["RSI14"]:
        events.append("RSIが70を下抜けて買われすぎゾーンを脱出(勢いの一服)")

    past_window = df["Close"].iloc[-(NEW_HIGH_LOW_WINDOW + 1):-1]
    if cur["Close"] > past_window.max():
        events.append(f"{NEW_HIGH_LOW_WINDOW}日高値を更新")
    if cur["Close"] < past_window.min():
        events.append(f"{NEW_HIGH_LOW_WINDOW}日安値を更新")

    vol_avg = df["Volume"].iloc[-21:-1].mean()
    if vol_avg > 0 and cur["Volume"] >= vol_avg * VOLUME_SPIKE_RATIO:
        direction = "上昇" if cur["change_pct"] > 0 else "下落"
        events.append(f"出来高急増(平均の{cur['Volume'] / vol_avg:.1f}倍)を伴う{direction}")
    return events


def analyze_one_stock(code: str, name: str, api_key: str):
    """戻り値: (分析結果の辞書, 指標計算済みのDataFrame)。失敗時は None。"""
    global LAST_FETCH_ERROR
    to_dt = fetch_data.latest_available_date()
    from_dt = to_dt - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    try:
        df = fetch_data.fetch_daily_bars(code, from_dt.strftime("%Y%m%d"), to_dt.strftime("%Y%m%d"), api_key)
    except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で全体を止めないため
        LAST_FETCH_ERROR = str(e)[:200]
        print(f"[WARN] {code}({name}): データ取得に失敗しました: {e}")
        return None

    if df.empty or len(df) < 80:
        print(f"[WARN] {code}({name}): データが不足しています({len(df)}行)。スキップします。")
        return None

    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    df = indicators.add_moving_averages(df, "Close")
    df = indicators.add_rsi(df, "Close")
    df = indicators.add_daily_change(df, "Close")
    df = indicators.add_golden_cross_signal(df, "MA25", "MA75")
    df = indicators.add_dead_cross_signal(df, "MA25", "MA75")

    df["dev25"] = (df["Close"] - df["MA25"]) / df["MA25"] * 100
    range_hi = df["Close"].rolling(RANGE_WINDOW, min_periods=60).max()
    range_lo = df["Close"].rolling(RANGE_WINDOW, min_periods=60).min()
    span = (range_hi - range_lo).replace(0, np.nan)
    df["range_pos"] = ((df["Close"] - range_lo) / span * 100).fillna(50)

    # テクニカル強弱スコア(全期間ぶん計算。仮想運用でも過去の各日のスコアを使う)
    trend_pts = ((df["Close"] > df["MA25"]).astype(int) + (df["MA25"] > df["MA75"]).astype(int)
                 + (df["MA25"] > df["MA25"].shift(5)).astype(int)) / 3 * 100
    df["score"] = ((trend_pts + df["RSI14"] + df["range_pos"]) / 3).round()

    cur = df.iloc[-1]
    vol_avg20 = df["Volume"].iloc[-21:-1].mean()

    if cur["Close"] > cur["MA25"] > cur["MA75"]:
        trend = "上昇"
    elif cur["Close"] < cur["MA25"] < cur["MA75"]:
        trend = "下降"
    else:
        trend = "もみ合い"

    score = int(cur["score"])

    return {
        "code": code,
        "name": name,
        "date": cur["Date"].date(),
        "close": cur["Close"],
        "change_pct": cur["change_pct"],
        "rsi": cur["RSI14"],
        "dev25": cur["dev25"],
        "range_pos": cur["range_pos"],
        "vol20": df["change_pct"].abs().iloc[-20:].mean(),
        "volume_ratio": (cur["Volume"] / vol_avg20) if vol_avg20 > 0 else np.nan,
        "trend": trend,
        "trend_up": bool(cur["MA25"] > cur["MA75"]),
        "score": score,
        "golden_cross": bool(cur["golden_cross_MA25_MA75"]),
        "dead_cross": bool(cur["dead_cross_MA25_MA75"]),
        "rsi_oversold": bool(cur["RSI14"] <= RSI_OVERSOLD_TH),
        "rsi_overbought": bool(cur["RSI14"] >= RSI_OVERBOUGHT_TH),
        "dev_hot": bool(cur["dev25"] >= DEV_HOT_TH),
        "events": detect_events(df),
        "stats_oversold": compute_forward_return_stats(df, df["RSI14"] <= RSI_OVERSOLD_TH, FORWARD_RETURN_HORIZON),
        "stats_overbought": compute_forward_return_stats(df, df["RSI14"] >= RSI_OVERBOUGHT_TH, FORWARD_RETURN_HORIZON),
        "stats_golden_cross": compute_forward_return_stats(df, df["golden_cross_MA25_MA75"], FORWARD_RETURN_HORIZON),
        "stats_dev_hot": compute_forward_return_stats(df, df["dev25"] >= DEV_HOT_TH, FORWARD_RETURN_HORIZON),
    }, df


# =====================================================================
# メッセージ組み立て
# =====================================================================

def classify_breadth(pct: float) -> tuple[str, str]:
    if pct >= BREADTH_STRONG_TH:
        return "強い(追い風)", "🟢"
    if pct < BREADTH_WEAK_TH:
        return "弱い(逆風)", "🔴"
    return "普通(様子見)", "🟡"


def short(code: str) -> str:
    return code[:4]


def build_summary_bubble(results, failed, breadth_pct, run_dt) -> str:
    data_date = max(r["date"] for r in results).isoformat()
    head = f"📊 株価デイリーレポート {run_dt.month}/{run_dt.day}({WEEKDAY_JA[run_dt.weekday()]})\n"
    if fetch_data.DELAY_WEEKS > 0:
        head += (f"データ基準日: {data_date}\n"
                 f"※無料プランのため約{fetch_data.DELAY_WEEKS}週間前のデータ(練習・学習用)")
    else:
        head += f"データ基準日: {data_date}"

    up = sum(1 for r in results if r["trend_up"])
    label, emoji = classify_breadth(breadth_pct)
    breadth = f"🌡 相場地合い: {emoji}{label}\n{len(results)}銘柄中{up}銘柄が上昇トレンド({breadth_pct:.0f}%)"

    buys = [r for r in results if r["rsi_oversold"] or r["golden_cross"]]
    if buys:
        lines = [f"🎯 買いチャンス候補({len(buys)}件)"]
        for r in buys:
            lines.append(f"・{r['name']}({short(r['code'])})")
            if r["rsi_oversold"]:
                lines.append(f"  RSI {r['rsi']:.0f}(売られすぎ)\n  └{format_stat(r['stats_oversold'])}")
            if r["golden_cross"]:
                lines.append(f"  🟢ゴールデンクロス発生\n  └{format_stat(r['stats_golden_cross'])}")
        if breadth_pct < BREADTH_WEAK_TH:
            lines.append("⚠ 地合いが弱い日の逆張りは失敗しやすいので、いつも以上に慎重に")
        buy_block = "\n".join(lines)
    else:
        buy_block = "🎯 買いチャンス候補: なし"

    hots = [r for r in results if r["rsi_overbought"] or r["dev_hot"]]
    hot_block = ""
    if hots:
        lines = [f"⚠ 過熱・注意({len(hots)}件)"]
        for r in hots:
            lines.append(f"・{r['name']}({short(r['code'])})")
            if r["rsi_overbought"]:
                lines.append(f"  RSI {r['rsi']:.0f}(買われすぎ)\n  └{format_stat(r['stats_overbought'])}")
            if r["dev_hot"]:
                lines.append(f"  25日線から{r['dev25']:+.1f}%乖離\n  └{format_stat(r['stats_dev_hot'])}")
        hot_block = "\n".join(lines)

    ev_lines = [f"・{r['name']}: {e}" for r in results for e in r["events"]]
    if ev_lines:
        event_block = "🆕 今日の変化(前日比)\n" + "\n".join(ev_lines[:12])
        if len(ev_lines) > 12:
            event_block += f"\n…ほか{len(ev_lines) - 12}件"
    else:
        event_block = "🆕 今日の変化(前日比)\n大きな変化はありませんでした"

    ranked = sorted(results, key=lambda r: r["score"], reverse=True)
    top3 = " / ".join(f"{i + 1}.{r['name']} {r['score']}" for i, r in enumerate(ranked[:3]))
    bottom3 = " / ".join(f"{i + 1}.{r['name']} {r['score']}" for i, r in enumerate(ranked[::-1][:3]))
    rank_block = f"🏆 テクニカル強弱スコア(0〜100)\n強い順: {top3}\n弱い順: {bottom3}"

    blocks = [head, breadth, buy_block]
    if hot_block:
        blocks.append(hot_block)
    blocks += [event_block, rank_block]
    if failed:
        blocks.append(f"(取得失敗: {', '.join(failed)})")
    return SEP.join(blocks)


def tags_of(r) -> str:
    tags = []
    if r["golden_cross"]:
        tags.append("🟢GC")
    if r["dead_cross"]:
        tags.append("🔴DC")
    if r["rsi_oversold"]:
        tags.append("🔵売られすぎ")
    if r["rsi_overbought"]:
        tags.append("⚠買われすぎ")
    if r["dev_hot"]:
        tags.append("⚠乖離大")
    return " ".join(tags)


def build_detail_bubble(results) -> str:
    trend_icon = {"上昇": "↗上昇", "下降": "↘下降", "もみ合い": "→もみ合い"}
    lines = [f"📋 銘柄別データ({len(results)}銘柄・スコア順)",
             "見方: RSI / 乖離=25日線との差 / 位置=1年の値幅の何%か / 変動=1日の平均値幅"]
    for r in sorted(results, key=lambda r: r["score"], reverse=True):
        arrow = "▲" if r["change_pct"] > 0 else ("▼" if r["change_pct"] < 0 else "→")
        vol_ratio = "" if pd.isna(r["volume_ratio"]) else f" / 出来高{r['volume_ratio']:.1f}倍"
        tag = tags_of(r)
        lines.append(
            f"\n■{r['name']}({short(r['code'])}) スコア{r['score']}\n"
            f" {r['close']:,.0f}円({arrow}{r['change_pct']:+.1f}%) {trend_icon[r['trend']]}\n"
            f" RSI{r['rsi']:.0f} / 乖離{r['dev25']:+.1f}% / 位置{r['range_pos']:.0f}%\n"
            f" 変動{r['vol20']:.1f}%{vol_ratio}"
            + (f"\n {tag}" if tag else "")
        )
    return "\n".join(lines)


def sizing_line(price: float) -> str:
    """授業料予算と1回の損失上限から、『もし買うなら』の損切りラインと最大株数を出す。"""
    stop_pct = paper_trading.STOP_LOSS_PCT
    max_loss = TUITION_BUDGET_YEN * MAX_LOSS_PCT_PER_TRADE / 100
    max_position = max_loss / (stop_pct / 100)
    shares = int(max_position // price)
    stop_price = price * (1 - stop_pct / 100)
    if shares == 0:
        return (f"  もし買うなら: 1株{price:,.0f}円が上限{max_position:,.0f}円を超えるので見送り"
                f"(予算に対して値がさすぎる)")
    return (f"  もし買うなら: 損切り{stop_price:,.0f}円(-{stop_pct:.0f}%)で損失を{max_loss:,.0f}円以内に"
            f" → 最大{shares}株(約{shares * price:,.0f}円)")


def interpret(r: dict) -> str:
    if r["rsi_overbought"] or r["dev_hot"]:
        return "勢いは強いが過熱気味。飛びつかず、25日線付近までの押し目を待つ場面とされる"
    if r["trend"] == "上昇":
        return "上昇トレンドが続いている。順張りなら候補になる形"
    if r["trend"] == "下降":
        return "大きな流れはまだ下向き。一時的な反発(ダマシ)の可能性も"
    return "方向感はまだ定まっていない(もみ合い)"


def build_scan_bubble(scan, deep) -> str:
    if scan is None:
        return "🔭 市場スキャン\n今日は全銘柄データが取得できませんでした(次回に再挑戦します)"

    def n4(c):
        return c[:4]

    lines = [f"🔭 市場スキャン(東証{market_scan.TARGET_MARKET}・売買代金"
             f"{market_scan.MIN_TURNOVER_YEN // 100_000_000}億円以上 {scan['universe_size']}銘柄)",
             f"値上がり銘柄の割合: {scan['advance_ratio']:.0f}% / 3ヶ月前より高い銘柄: {scan['above_3m_ago_ratio']:.0f}%"]
    lines.append("\n🚀 3ヶ月で強い株 TOP5")
    for i, r in enumerate(scan["momentum"], 1):
        lines.append(f"{i}.{r['name']}({n4(r['code'])}) {r['ret60']:+.0f}%(1ヶ月{r['ret20']:+.0f}%)")
    lines.append("\n🔥 売買代金が急増 TOP3")
    for i, r in enumerate(scan["turnover_spike"], 1):
        lines.append(f"{i}.{r['name']}({n4(r['code'])}) 普段の{r['turn_ratio']:.1f}倍 / 当日{r['ret1']:+.1f}%")
    lines.append("\n📈 値上がり率: " + " / ".join(f"{r['name']}{r['ret1']:+.1f}%" for r in scan["gainers"]))
    lines.append("📉 値下がり率: " + " / ".join(f"{r['name']}{r['ret1']:+.1f}%" for r in scan["losers"]))

    if deep:
        lines.append("\n🔎 深掘り(候補をテクニカルで確認)")
        for c, r in deep:
            lines.append(f"・{c['name']}({n4(c['code'])}) {c.get('sector', '')}【{c['why']}】\n"
                         f"  {r['trend']}トレンド / RSI{r['rsi']:.0f} / 乖離{r['dev25']:+.1f}% / 1年の位置{r['range_pos']:.0f}%\n"
                         f"  読み: {interpret(r)}\n{sizing_line(r['close'])}")
        lines.append(f"\n※資金管理: 授業料予算{TUITION_BUDGET_YEN:,}円・1回の損失上限{MAX_LOSS_PCT_PER_TRADE:.0f}%で計算"
                     "(GitHubのVariablesで変更可)。買う推奨ではなく、買うならこの範囲で、という目安です")
    return "\n".join(lines)


def build_paper_bubble(paper) -> str:
    if paper is None:
        return "💼 仮想運用\nデータ不足のため、今日は計算できませんでした。"
    lines = [
        f"💼 仮想運用(元手{paper_trading.INITIAL_CAPITAL // 10000}万円・{paper['start']:%Y/%m/%d}〜)",
        "※実際のお金は動いていません。ルールの実力を確かめる練習試合です",
    ]
    icons = ["📈", "📉", "🧺"]
    for icon, s in zip(icons, paper["strategies"]):
        wr = "" if s["win_rate_pct"] is None else f" 勝率{s['win_rate_pct']:.0f}%"
        lines.append(f"\n{icon}{s['name']}\n {s['equity']:,.0f}円 ({s['total_return_pct']:+.1f}%)"
                     f" / 最大下落{s['max_drawdown_pct']:.1f}% / 取引{s['n_trades']}回{wr}")
        if s["holdings"] and icon != "🧺":
            held = " / ".join(f"{h['name']}({h['unrealized_pct']:+.1f}%)" for h in s["holdings"])
            lines.append(f" 保有: {held}")

    bh = paper["strategies"][-1]["total_return_pct"]
    beat = [s["name"].split("(")[0] for s in paper["strategies"][:-1] if s["total_return_pct"] > bh]
    lines.append("\n🏁 " + (f"{'・'.join(beat)}が「放置」に勝っています" if beat
                            else "今のところ、どのルールも「放置」に勝てていません"))

    acts = []
    for s in paper["strategies"][:-1]:
        label = s["name"].split("(")[0]
        acts += [f"・{label}: {t}" for t in s["today_fills"]]
        acts += [f"・{label}: 次の寄付で{t}" for t in s["next_orders"]]
    lines.append("\n🆕 今日の仮想売買\n" + ("\n".join(acts) if acts else "売買なし"))

    lines.append(f"\nルール確定後の検証日数: {paper['forward_days']}営業日"
                 "(これより前は過去データでの検証。本当の実力が分かるのは確定後の成績)")
    return "\n".join(lines)


def build_learning_bubble(results, breadth_pct, run_dt, paper=None) -> str:
    # 営業日ベースで1語ずつ進める(土日で用語が飛ばされないように)
    day_index = int(np.busday_count("2026-01-05", run_dt.date()))
    term, explanation, example_fn = GLOSSARY_POOL[day_index % len(GLOSSARY_POOL)]
    learn = f"📚 今日の用語: {term}\n{explanation}"
    if example_fn is not None:
        try:
            learn += f"\n👀 今日のデータでは: {example_fn(results, paper)}"
        except (IndexError, KeyError, ValueError, TypeError):
            pass  # 仮想運用が計算できなかった日など。例示なしで送る

    actions = ["★今日チェックすべきアクション"]
    buys = [r for r in results if r["rsi_oversold"] or r["golden_cross"]]
    hots = [r for r in results if r["rsi_overbought"] or r["dev_hot"]]
    if buys:
        names = "・".join(r["name"] for r in buys[:3])
        actions.append(f"・{names}のチャートを開き、下げ止まり(安値を切り下げなくなったか)と出来高の増加を確認しよう")
    if hots:
        names = "・".join(r["name"] for r in hots[:3])
        actions.append(f"・{names}は過熱気味。一般に、保有中なら利益確定を検討する場面、未保有なら飛びつかず様子を見る場面とされる")
    movers = [r for r in results if r["events"]]
    if movers:
        top = max(movers, key=lambda r: len(r["events"]))
        actions.append(f"・今日一番動きがあったのは{top['name']}。「🆕今日の変化」がなぜ起きたか、ニュースも合わせて調べてみよう")
    if not buys and not hots:
        actions.append("・明確なシグナルがない日は「何もしない」のも立派な判断。スコア上位/下位の銘柄のチャートを見比べて、強い株と弱い株の形の違いを観察してみよう")
    label, _ = classify_breadth(breadth_pct)
    actions.append(f"・地合いは「{label}」。個別のシグナルは必ず地合いとセットで解釈しよう")
    actions.append("・実際に買うなら、その前にメモに3行書こう: ①なぜ上がると思うか ②どうなったら間違いと認めるか(損切り価格)"
                   " ③いつ見直すか。これがあると、負けても『どの読みが外れたか』が分かる＝意味のある失敗になる")

    disclaimer = ("※スコア・過去実績は過去データの機械的な集計です。"
                  "将来の値動きを保証するものでも、投資助言でもありません。")
    return SEP.join([learn, "\n".join(actions), disclaimer])


# =====================================================================
# メイン
# =====================================================================

def main():
    api_key = fetch_data.get_api_key()
    line_token, line_user_id = get_line_credentials()
    run_dt = datetime.now(JST)

    print(f"監視銘柄 {len(WATCHLIST)}件を分析します"
          f"(レート制限のため1銘柄あたり約{fetch_data.MIN_REQUEST_INTERVAL_SEC:.0f}秒間隔)...")

    results, failed, dfs = [], [], {}
    for code, name in WATCHLIST.items():
        out = analyze_one_stock(code, name, api_key)
        if out is None:
            failed.append(name)
            continue
        r, df = out
        results.append(r)
        dfs[code] = df
        print(f"  {code}({name}): 終値{r['close']:,.0f}円 / RSI{r['rsi']:.0f} / スコア{r['score']} / 変化{len(r['events'])}件")

    if not results:
        print("全銘柄でデータ取得に失敗しました。")
        alert = ("⚠️ 本日の株レポートを作れませんでした。\n"
                 "全銘柄のデータ取得に失敗しました。\n"
                 f"原因(APIの返答): {LAST_FETCH_ERROR or '不明'}\n"
                 "GitHubの Actions タブでログを確認してください。")
        try:
            send_line_messages([alert], line_token, line_user_id)
        except Exception as e:  # noqa: BLE001 - お知らせの送信失敗で、本来のエラー終了を隠さない
            print(f"[WARN] 失敗のお知らせをLINEに送れませんでした: {e}")
        sys.exit(1)  # Actions上で失敗(赤)として見えるようにする

    breadth_pct = sum(r["trend_up"] for r in results) / len(results) * 100

    try:
        paper = paper_trading.run_paper_trading(dfs, {r["code"]: r["name"] for r in results})
    except Exception as e:  # noqa: BLE001 - 仮想運用の不具合でレポート全体を止めない
        print(f"[WARN] 仮想運用の計算に失敗しました: {e}")
        paper = None

    # 市場スキャン: 監視銘柄の日付を営業日カレンダーとして使う(追加のAPI呼び出し不要)
    scan, deep = None, []
    try:
        calendar = sorted(max(dfs.values(), key=len)["Date"].tolist())
        print("市場スキャン中(東証プライム全体)...")
        scan = market_scan.run_market_scan(calendar, api_key)
        if scan:
            for c in market_scan.deep_dive_candidates(scan, exclude=set(WATCHLIST)):
                out = analyze_one_stock(c["code"], c["name"], api_key)
                if out is not None:
                    deep.append((c, out[0]))
    except Exception as e:  # noqa: BLE001 - スキャンの不具合でレポート全体を止めない
        print(f"[WARN] 市場スキャンに失敗しました: {e}")
        scan = None

    bubbles = [
        build_summary_bubble(results, failed, breadth_pct, run_dt),
        build_scan_bubble(scan, deep),
        build_paper_bubble(paper),
        build_detail_bubble(results),
        build_learning_bubble(results, breadth_pct, run_dt, paper),
    ]
    sent = send_line_messages(bubbles, line_token, line_user_id)
    print("LINEへの送信に成功しました！" if sent else "LINE送信に失敗しました。上のDEBUG行を確認してください。")
    if not sent:
        sys.exit(1)


if __name__ == "__main__":
    main()
