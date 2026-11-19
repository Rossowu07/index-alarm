#!/usr/bin/env python3
"""盤中監控：加權／櫃買指數的「分鐘均值」乖離率 + 分鐘均值轉向，觸發時以 Discord 推播。

用法：
    pip install requests
    export DISCORD_WEBHOOK="頻道的 Webhook 網址 (等同密碼，請保密)"
    python index_alert.py --test    # 傳一則測試訊息
    python index_alert.py --dry     # 試跑，只印出訊息、不推播
    python index_alert.py           # 正式執行 (常駐，盤外自動休眠)
    python index_alert.py --ci      # GitHub Actions 用：單日執行，收盤或休市自動結束
"""
import csv
import os
import sys
import time
import datetime as dt
from collections import deque
from zoneinfo import ZoneInfo

import requests

# ====================== 可調參數 ======================
INDICES = {"tse_t00.tw": "加權指數", "otc_o00.tw": "櫃買指數"}
MA_N = 20                       # 以幾根「分鐘均值」計算移動平均
BIAS_PCT = {                    # 乖離率門檻 (%)，正負皆觸發。數值為暫定，請依實際觀察調整
    "tse_t00.tw": 0.30,
    "otc_o00.tw": 0.40,
}
TURN_CONFIRM = 2                # 轉向確認：需連續幾根分鐘均值同向才算轉向 (1 = 最靈敏、雜訊最多)
COOLDOWN_MIN = 15               # 同一指數、同方向的乖離率提示，冷卻分鐘數
POLL_SEC = 5                    # 輪詢秒數 (指數約 5 秒更新一次)
START = dt.time(9, 0)
END = dt.time(13, 25)           # 13:25 起為收盤試撮，預設不納入；要含尾盤可改 13:30
STALE_SEC = 90                  # 報價時間落後超過此秒數視為無效 (休市日會自動略過)
HOLIDAY_GRACE = dt.time(9, 10)  # --ci 模式：此時間後仍無有效報價，視為休市或連線被拒，直接結束
NODATA_WAIT_SEC = 120           # --ci 模式：程式啟動後至少嘗試取價這麼久，才可判定無資料
# ======================================================

TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "Mozilla/5.0"}
MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
DISCORD_HOSTS = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")


def new_session():
    s = requests.Session()
    s.headers.update(UA)
    s.get(MIS_HOME, timeout=10)  # 先取得 cookie，否則 API 常回空
    return s


def fetch(s):
    params = {
        "ex_ch": "|".join(INDICES.keys()),
        "json": "1",
        "delay": "0",
        "_": int(time.time() * 1000),
    }
    r = s.get(MIS_API, params=params, timeout=10)
    r.raise_for_status()
    out = {}
    for row in r.json().get("msgArray", []):
        key = f"{row.get('ex')}_{row.get('ch')}"
        z = row.get("z")
        if key not in INDICES or z in (None, "", "-"):
            continue
        ts = dt.datetime.fromtimestamp(int(row["tlong"]) / 1000, TZ)
        out[key] = (ts, float(z))
    return out


def push(text, dry):
    print(f"[{dt.datetime.now(TZ):%H:%M:%S}] {text}", flush=True)
    if dry:
        return
    url = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not url:
        print("缺少 DISCORD_WEBHOOK，未推播", file=sys.stderr)
        return
    if not url.startswith(DISCORD_HOSTS):
        print("DISCORD_WEBHOOK 格式不對，應以 https://discord.com/api/webhooks/ 開頭", file=sys.stderr)
        return
    for attempt in range(2):
        try:
            r = requests.post(
                url,
                json={"content": text[:1900], "allowed_mentions": {"parse": []}},
                timeout=10,
            )
            if r.status_code == 429 and attempt == 0:  # 被限流，依指示等待後重試一次
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 1)
                continue
            if r.status_code not in (200, 204):
                print(f"Discord 推播失敗 {r.status_code}: {r.text}", file=sys.stderr)
            return
        except requests.RequestException as e:
            print(f"Discord 推播例外: {e}", file=sys.stderr)
            return


class State:
    def __init__(self, code, day):
        self.code, self.name, self.day = code, INDICES[code], day
        self.avgs = deque(maxlen=MA_N + 10)
        self.cur = None
        self.ticks = []
        self.last_ts = None
        self.trend = 0
        self.last_alert = {}
        self.path = f"bars_{day:%Y%m%d}_{code}.csv"
        self._load()

    def _load(self):
        """程式中途重啟時，載入今日已存的分鐘均值，避免重新暖機。"""
        if not os.path.exists(self.path):
            return
        with open(self.path, newline="") as f:
            for row in csv.reader(f):
                self.avgs.append(float(row[1]))

    def feed(self, ts, price):
        if ts == self.last_ts:  # 同一筆報價，略過
            return []
        self.last_ts = ts
        m = ts.replace(second=0, microsecond=0)
        msgs = []
        if self.cur is not None and m != self.cur and self.ticks:
            msgs = self._close_bar()
            self.ticks = []
        self.cur = m
        self.ticks.append(price)
        return msgs

    def _cooldown_ok(self, side, now):
        last = self.last_alert.get(side)
        if last and (now - last).total_seconds() < COOLDOWN_MIN * 60:
            return False
        self.last_alert[side] = now
        return True

    def _close_bar(self):
        avg = sum(self.ticks) / len(self.ticks)
        self.avgs.append(avg)
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow([f"{self.cur:%H:%M}", f"{avg:.2f}"])

        hist = list(self.avgs)
        msgs, hhmm = [], f"{self.cur:%H:%M}"

        # 1) 乖離率：最新分鐘均值 vs. 最近 MA_N 根分鐘均值的平均
        if len(hist) >= MA_N:
            ma = sum(hist[-MA_N:]) / MA_N
            bias = (avg - ma) / ma * 100
            print(f"  {hhmm} {self.name} 均值 {avg:.2f}｜{MA_N}分MA {ma:.2f}｜乖離 {bias:+.2f}%", flush=True)
            th = BIAS_PCT[self.code]
            side = "正" if bias >= th else "負" if bias <= -th else None
            if side and self._cooldown_ok(side, self.cur):
                msgs.append(
                    f"【{self.name}】{hhmm} {side}乖離 {bias:+.2f}%\n"
                    f"分鐘均值 {avg:.2f}｜{MA_N}分MA {ma:.2f}｜門檻 ±{th}%"
                )
        else:
            print(f"  {hhmm} {self.name} 均值 {avg:.2f}｜暖機 {len(hist)}/{MA_N}", flush=True)

        # 2) 分鐘均值轉向：連續 TURN_CONFIRM 根同向，且與先前確認的趨勢相反
        k = TURN_CONFIRM
        if len(hist) >= k + 1:
            w = hist[-(k + 1):]
            ds = [w[i + 1] - w[i] for i in range(k)]
            s = 1 if all(d > 0 for d in ds) else -1 if all(d < 0 for d in ds) else 0
            if s != 0:
                if self.trend != 0 and s != self.trend:
                    word = "由跌轉漲 ▲" if s > 0 else "由漲轉跌 ▼"
                    msgs.append(
                        f"【{self.name}】{hhmm} 分鐘均值{word}\n"
                        f"近{k + 1}根：" + " → ".join(f"{x:.2f}" for x in w)
                    )
                self.trend = s
        return msgs


def ci_action(now, got_data, t0):
    """--ci 模式 (GitHub Actions)：單日執行一輪就結束。
    回傳 wait=等開盤、run=盤中監控、closed=已收盤或週末、
    nodata=09:10 後，且程式已實際嘗試取價至少 NODATA_WAIT_SEC 秒，仍無有效報價 (休市日或取不到資料)。
    t0 為程式啟動時間：晚於 09:10 才手動啟動時，也會先嘗試取價，不會直接判定無資料。"""
    if now.weekday() >= 5 or now.time() > END:
        return "closed"
    if now.time() < START:
        return "wait"
    if (not got_data and now.time() >= HOLIDAY_GRACE
            and (now - t0).total_seconds() >= NODATA_WAIT_SEC):
        return "nodata"
    return "run"


def main():
    if "--test" in sys.argv:
        push("✅ 測試訊息：Discord 推播設定成功", dry=False)
        return
    dry = "--dry" in sys.argv
    ci = "--ci" in sys.argv
    states, state_day = {}, None
    sess = None
    got_data = False
    started = False
    t0 = dt.datetime.now(TZ)
    print(f"啟動：MA={MA_N}分、轉向確認={TURN_CONFIRM}根、{'試跑模式' if dry else '推播模式'}"
          f"{'、CI 單日模式' if ci else ''}", flush=True)

    while True:
        now = dt.datetime.now(TZ)
        if ci:
            act = ci_action(now, got_data, t0)
            if act == "closed":
                if started:
                    push(f"⏹ 今日監控結束 ({now:%H:%M})", dry)
                else:
                    print("週末或已過收盤，不執行", flush=True)
                return
            if act == "nodata":
                push(f"⚠️ {now:%H:%M} 仍取不到有效報價：可能是休市日，或證交所 MIS 拒絕此主機連線。今日監控結束。", dry)
                return
            if act == "wait":
                time.sleep(15)
                continue
            if not started:
                started = True
                late = "（晚於開盤，乖離率需再累積分鐘資料）" if now.time() > dt.time(9, 3) else ""
                push(f"▶ 監控啟動 {now:%H:%M}{late}", dry)
        elif now.weekday() >= 5 or not (START <= now.time() <= END):
            time.sleep(30)
            continue
        if state_day != now.date():  # 新的一天，重建狀態
            state_day = now.date()
            states = {c: State(c, state_day) for c in INDICES}
            sess = None
        try:
            if sess is None:
                sess = new_session()
            data = fetch(sess)
        except Exception as e:
            print(f"取價失敗：{e}", file=sys.stderr)
            sess = None
            time.sleep(POLL_SEC * 2)
            continue

        for code, (ts, price) in data.items():
            if abs((now - ts).total_seconds()) > STALE_SEC:
                continue  # 報價過舊 (休市日或資料中斷)
            got_data = True
            for msg in states[code].feed(ts, price):
                push(msg, dry)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
