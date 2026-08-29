#!/usr/bin/env python3
"""每日估值推送 — 动态计算建仓/重仓线，微信 Server酱"""
import os
from curl_cffi import requests
import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime

# SENDKEY 从环境变量读取（本地 launchd 用 push_wrapper.sh 注入，云端用 GitHub Secrets）
SENDKEY = os.environ.get("SENDKEY", "").strip()
if not SENDKEY:
    raise SystemExit("❌ 未设置 SENDKEY 环境变量")

# 配置：名称 → (东方财富代码, baostock代码, 指标, 窗口交易日, 建仓分位, 重仓分位)
CONFIG = [
    ("长江电力", "1.600900", "sh.600900", "PE", 1250, 15, 10, 4000),   # 5年
    ("工商银行", "1.601398", "sh.601398", "PB", 500,  25, 15, 8000),   # 2年
    ("中国移动", "1.600941", "sh.600941", "PE", 750,  15, 10, 500),    # 3年
    ("美的集团", "0.000333", "sz.000333", "PE", 1250, 15, 10, 500),    # 5年
    ("粤高速",   "0.000429", "sz.000429", "PE", 500,  15, 10, 1500),   # 2年
    ("宁波银行", "0.002142", "sz.002142", "PB", 500,  25, 15, 1000),   # 2年
    ("平安银行", "0.000001", "sz.000001", "PB", 500,  25, 15, 4000),   # 2年
    ("云南白药", "0.000538", "sz.000538", "PE", 1250, 15, 10, 600),    # 5年
]

url = "https://push2.eastmoney.com/api/qt/stock/get"

# 1. 动态计算建仓/重仓线
thresholds = {}
for name, em, bsid, metric, window, buy_pct, heavy_pct, shares in CONFIG:
    bs.login()
    rs = bs.query_history_k_data_plus(bsid, "date,close,peTTM,pbMRQ",
        start_date="2000-01-01", end_date=datetime.now().strftime("%Y-%m-%d"),
        frequency="d", adjustflag="2")
    rows_list = []
    while rs.next(): rows_list.append(rs.get_row_data())
    bs.logout()
    df = pd.DataFrame(rows_list, columns=["日期","收盘价","PE","PB"])
    for c in ["收盘价","PE","PB"]: df[c] = pd.to_numeric(df[c], errors="coerce")

    col = metric
    vals = df[col].dropna().tail(window)

    decimals = 3 if metric == "PB" else 2  # PB 三位小数，PE 两位

    buy_val = round(float(np.percentile(vals, buy_pct)), decimals)
    heavy_val = round(float(np.percentile(vals, heavy_pct)), decimals)

    # 近一年 20%/10% 分位做下限保护：取二者更低，防止估值下行时误判
    vals_1y = df[col].dropna().tail(250)
    buy_1y = round(float(np.percentile(vals_1y, 20)), decimals) if len(vals_1y) >= 50 else buy_val
    heavy_1y = round(float(np.percentile(vals_1y, 10)), decimals) if len(vals_1y) >= 50 else heavy_val

    buy_final = min(buy_val, buy_1y)
    heavy_final = min(heavy_val, heavy_1y)

    thresholds[name] = {
        "buy": buy_final, "heavy": heavy_final,
        "buy_pct": buy_pct, "heavy_pct": heavy_pct,
    }

# 2. 实时行情 + 生成表格
table = "| 股票 | 股数 | 现价 | PE/PB | 建仓价 | 建仓 | 重仓价 | 重仓 | 状态 |\n"
table += "|------|------|-----|-------|-------|------|-------|------|------|\n"

for name, em, bsid, metric, window, buy_pct, heavy_pct, shares in CONFIG:
    r = requests.get(url, params={"secid": em, "fields": "f43,f164,f167,f170"},
                     impersonate="chrome110", timeout=10)
    d = r.json().get('data', {}) or {}
    cur_price = d.get('f43', 0) / 100 or 0
    pe = d.get('f164', 0) / 100 or None
    pb_em = d.get('f167', 0) / 100 or None

    t = thresholds[name]
    is_pb = metric == "PB"
    cur_val = pb_em if is_pb else pe  # 当前估值统一用东方财富数据
    label = "PB" if is_pb else "PE"

    buy_val = t["buy"]
    heavy_val = t["heavy"]
    buy_price = round(cur_price * buy_val / cur_val, 2) if cur_val else 0
    heavy_price = round(cur_price * heavy_val / cur_val, 2) if cur_val else 0

    if cur_val and cur_val <= heavy_val:
        status = "✅重仓区"
    elif cur_val and cur_val <= buy_val:
        gap2 = round((cur_val - heavy_val) / cur_val * 100)
        diff2 = round(cur_price - heavy_price, 2)
        status = f"🟢距重仓差{gap2}%(还差¥{diff2:.2f})"
    else:
        gap = round((cur_val - buy_val) / cur_val * 100)
        diff = round(cur_price - buy_price, 2)
        status = f"距建仓差{gap}%(还差¥{diff:.2f})"

    # 当前PB显示东方财富两位，建仓/重仓PB保留三位精度
    cv = f"{pb_em:.2f}" if is_pb else f"{cur_val:.2f}"
    bv = f"{buy_val:.3f}" if is_pb else f"{buy_val:.2f}"
    hv = f"{heavy_val:.3f}" if is_pb else f"{heavy_val:.2f}"
    table += f"| {name} | {shares} | {cur_price:.2f} | {label}={cv} | {buy_price} | {label}={bv} | {heavy_price} | {label}={hv} | {status} |\n"

body = f"📊 估值监控 {datetime.now().strftime('%m-%d %H:%M')}\n\n{table}"

# 3. 推送到微信
r = requests.post(f"https://sctapi.ftqq.com/{SENDKEY}.send",
                  json={"title": f"📊 估值监控 {datetime.now().strftime('%m-%d %H:%M')}",
                        "desp": body},
                  impersonate="chrome110", timeout=10)
print(f"推送: {r.json()['data']['error']}")
print(body)
