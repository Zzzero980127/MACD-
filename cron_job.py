def fetch_finmind_data(stock_info):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
        df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
        df = df.dropna(subset=['Close'])
        
        if len(df) < 35: return None

        # 計算 MACD
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        osc_today = float(df.iloc[-1]['OSC'])
        osc_prev = float(df.iloc[-2]['OSC'])

        close_price = float(df.iloc[-1]['Close'])
        prev_close = float(df.iloc[-2]['Close'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # 防追高安全機制：若當日拉大陽線超過 6%，直接排除
        if pct_change > 6.0:
            return None

        # --- 💯 加分演算機制啟動 ---
        score = 0
        tags = []

        # 條件 A：MACD 轉折起漲 (綠柱縮短 / 剛轉紅柱)
        is_green_shrinking = (osc_today < 0) and (osc_today > osc_prev)
        is_first_red = (osc_today > 0) and (osc_prev <= 0)
        is_macd_expanding = (osc_today > 0) and (osc_today > osc_prev)

        if is_green_shrinking:
            score += 20
            macd_status = "📉 綠柱縮短"
        elif is_first_red:
            score += 25
            macd_status = "💥 紅柱第1天"
        elif is_macd_expanding:
            score += 15
            macd_status = "🔥 紅柱擴大"
        else:
            return None # 若連基本轉折/擴大都沒有，則不給分直接剔除

        # 條件 B：籌碼分析與外資暴增（裕民策略）
        time.sleep(0.8)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
                daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index().sort_values('date')
                
                if len(daily_summary) >= 2:
                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    
                    # 1. 外資基礎加分 (由賣轉買 或 連買)
                    if today_foreign > 50 and prev_foreign <= 50:
                        score += 20
                        tags.append("🔄外資轉買")
                    elif today_foreign > 200 and prev_foreign > 200:
                        score += 25
                        tags.append("🔥外資連買")

                    # 2. 🔥 裕民策略（額外加分 35 分）：外資買超比昨日暴增 3 倍 + MACD 紅柱高於昨日
                    if (prev_foreign > 0) and (today_foreign >= prev_foreign * 3) and (today_foreign >= 500) and is_macd_expanding:
                        score += 35
                        tags.append("⚡外資爆買3倍")

                    # 3. 安全進場位階加分 (漲幅 1% ~ 4% 代表發動不久，最安全)
                    if 1.0 <= pct_change <= 4.0:
                        score += 15
                        tags.append("🛡️黃金位階")

                    # 門檻：總得分需達到 40 分以上才列入精選
                    if score >= 40:
                        return {
                            "code": stock_id,
                            "name": stock_name,
                            "close": close_price,
                            "pct": pct_change,
                            "foreign_shares": round(today_foreign),
                            "score": score,
                            "status_label": " ".join(tags) if tags else "籌碼轉佳",
                            "macd_status": macd_status
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析異常: {e}", flush=True)

    return None
