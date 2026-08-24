def fetch_stock_price_with_retry(stock_id):
    """ 【選股專用】100% 強制使用 Token 抓取日 K 價量，絕不切換無 Token 通道 """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    url_token = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res = http.get(url_token, timeout=5.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 35: 
                return df
    except Exception as e:
        print(f"⚠️ [{stock_id}] 選股價量 API 失敗 (Token 專線): {e}", flush=True)

    return None

def fetch_foreign_investor_with_retry(stock_id):
    """ 【選股專用】100% 強制使用 Token 抓取外資籌碼 """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
    url_token = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res = http.get(url_token, timeout=5.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
                daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index()
                daily_summary = daily_summary.sort_values('date')
                
                if len(daily_summary) >= 2:
                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    
                    is_turn_to_buy = (today_foreign > 50) and (prev_foreign <= 50)
                    is_continuous_buy = (today_foreign > 200) and (prev_foreign > 200)

                    if is_turn_to_buy or is_continuous_buy:
                        status_label = "🔄 外資由賣轉買" if is_turn_to_buy else "🔥 外資連買加碼"
                        return True, round(today_foreign), status_label
                    else:
                        return False, round(today_foreign), ""
    except Exception as e:
        print(f"⚠️ [{stock_id}] 選股外資 API 失敗 (Token 專線): {e}", flush=True)

    return False, 0, ""
