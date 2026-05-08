import asyncio
import aiohttp
import sqlite3
import re
import os
import json
import random
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# =========================================================
# 系統設定與全域變數
# =========================================================
DB_FILE = 'data/mops_data.db' 
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def init_db():
    os.makedirs('data', exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS page1_data (
        stock_id TEXT, meeting_type TEXT, stock_name TEXT, meeting_date TEXT,
        sg_status TEXT, sg_item TEXT, condition TEXT, mops_status TEXT, mops_item TEXT, 
        update_date TEXT, price TEXT, last_buy TEXT, needs_debug INTEGER, PRIMARY KEY (stock_id, meeting_type))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS mops_states (state_key TEXT PRIMARY KEY, time TEXT, pending INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS stock_history (
        stock_id TEXT, year TEXT, status TEXT, first_status TEXT, latest_status TEXT, item TEXT, PRIMARY KEY (stock_id, year))''')
    conn.commit()
    return conn

# =========================================================
# 模組 2：股價、假日API與最後買進日計算
# =========================================================
async def fetch_taiwan_holidays(session):
    print("[Init] 📅 正在獲取台灣政府行政機關日曆表...")
    holidays = set()
    try:
        async with session.get("https://data.ntpc.gov.tw/api/datasets/308DCD75-6434-45BC-A950-5BEF4ADC36EA/json?page=0&size=1000", headers={'User-Agent': USER_AGENT}, ssl=False) as resp:
            text = await resp.text()
            if "<html>" in text.lower(): return holidays
            for item in json.loads(text):
                if item.get('isHoliday') == '是' and item.get('date'): holidays.add(item.get('date').replace('/', ''))
        print(f"   ✅ 成功獲取假日資料，共 {len(holidays)} 天假期")
    except: pass
    return holidays

async def fetch_twse_prices(session):
    print("[Init] 📈 正在從證交所/櫃買中心獲取最新股價...")
    price_dict = {}
    try:
        async with session.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers={'User-Agent': USER_AGENT}, ssl=False) as resp:
            for item in json.loads(await resp.text()): price_dict[item['Code']] = item['ClosingPrice']
        async with session.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", headers={'User-Agent': USER_AGENT}, ssl=False) as resp:
            for item in json.loads(await resp.text()): price_dict[item['SecuritiesCompanyCode']] = item['Close']
        print(f"   ✅ 成功獲取股價，共 {len(price_dict)} 檔")
    except: pass
    return price_dict

def calculate_last_buy_date(meeting_date_str, meeting_type, holidays_set):
    if not meeting_date_str: return ""
    try:
        roc_year, m, d = map(int, meeting_date_str.split('/'))
        meeting_date = datetime(roc_year + 1911, m, d)
        stop_start = meeting_date - timedelta(days=60 if "常會" in meeting_type else 30)
        settlement_date = stop_start - timedelta(days=1)
        while settlement_date.weekday() >= 5 or settlement_date.strftime("%Y%m%d") in holidays_set: settlement_date -= timedelta(days=1)
        last_buy = settlement_date
        trading_days = 2
        while trading_days > 0:
            last_buy -= timedelta(days=1)
            if last_buy.weekday() < 5 and last_buy.strftime("%Y%m%d") not in holidays_set: trading_days -= 1
        return f"{last_buy.strftime('%Y/%m/%d')} ({['一', '二', '三', '四', '五', '六', '日'][last_buy.weekday()]})"
    except: return ""

def get_mops_query_years():
    y, m = datetime.now().year - 1911, datetime.now().month
    if 1 <= m <= 2: return [{"year": y - 1, "month": "11"}, {"year": y - 1, "month": "12"}, {"year": y, "month": "all"}]
    elif 3 <= m <= 10: return [{"year": y, "month": "all"}]
    return [{"year": y, "month": "all"}, {"year": y + 1, "month": "all"}]

# =========================================================
# 強化版：紀念品字串分析工具
# =========================================================
def analyze_souvenir_status(text: str):
    if not text or len(text.strip()) < 10: 
        return ("無資料", "網頁未載入")
    
    # 清理字串，移除所有多餘空白
    text = re.sub(r'[\s\n\r　]+', ' ', text)
    flat_text = text.replace(' ', '')
    
    # 1. 先判定「不發放」的情形 (這通常很明確)
    if any(k in flat_text for k in ["不發放紀念品", "未發放紀念品", "本年度不發放"]):
        return ("不發放", "不發放")
    
    # 2. 判定「尚未決定」的情形
    if any(k in flat_text for k in ["尚未決定", "另行公告", "開會55日前"]):
        return ("待公布", "開會前另行公告")
    
    # 3. 判定「有發放」並嘗試抓取品項
    # 擴大搜尋範圍：包含「紀念品：」、「紀念品內容：」、「紀念品名稱為：」
    item_patterns = [
        r"紀念品(?:名稱|內容)?(?:為)?[:：]\s*(.*?)(?:[。，；\(\)]|$)",
        r"發放紀念品\s*[:：]\s*(.*?)(?:[。，；\(\)]|$)",
        r"提供紀念品\s*[:：]\s*(.*?)(?:[。，；\(\)]|$)"
    ]
    
    for p in item_patterns:
        match = re.search(p, text)
        if match:
            item = match.group(1).strip()
            # 二次清理：過濾掉常見的附註垃圾字眼
            for junk in ["(一)", "1.", "發放原則", "持股", "本公司", "請注意"]:
                item = item.split(junk)[0]
            if len(item) > 1:
                return ("發放", item.strip())
    
    # 4. 如果有提到發放但抓不到品項 (保底)
    if "發放紀念品" in flat_text:
        return ("發放", "有發放但格式特殊，請點開查看")
        
    return ("無資料", "查無紀念品關鍵字")

# =========================================================
# 🛡️ 模組 3：終極隱形網路請求
# =========================================================
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_html(session, url, method="GET", payload=None, referer=None):
    is_ajax = "ajax" in url
    headers = {
        'User-Agent': USER_AGENT,
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Connection': 'keep-alive',
    }
    
    if is_ajax:
        headers.update({
            'Accept': '*/*',
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Origin': 'https://mopsov.twse.com.tw',
        })
        if referer: headers['Referer'] = referer
    else:
        headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1'
        })
        
    try:
        if method == "POST":
            async with session.post(url, data=payload, headers=headers, timeout=20, ssl=False) as resp:
                resp.raise_for_status()
                return await resp.text(encoding='utf-8', errors='ignore')
        else:
            async with session.get(url, headers=headers, timeout=20, ssl=False) as resp:
                resp.raise_for_status()
                return await resp.text(encoding='utf-8', errors='ignore')
    except Exception as e:
        raise e

# =========================================================
# 爬蟲階段
# =========================================================
async def get_sg_data_async(session):
    print("[Step 1] 🚀 獲取零股寶基準資料...")
    sg_data = {}
    try:
        sg_html = await fetch_html(session, "https://stockgift.tw/STOCK/Suggest/Suggest")
        for tr in BeautifulSoup(sg_html, 'html.parser').select("#datatables tbody tr"):
            tds = tr.find_all('td')
            if len(tds) >= 7:
                res = re.search(r'\((\d+)\)', tds[0].text)
                if res: sg_data[res.group(1)] = {'常會': {'item': '無', 'condition': f"{tds[4].text.strip()} {tds[6].text.strip()}".strip()}}

        info_html = await fetch_html(session, "https://stockgift.tw/STOCK/Stock/Info")
        for table_id in ["hadEntrustdatatable", "hadEntrustdatatable2"]:
            table = BeautifulSoup(info_html, 'html.parser').find(id=table_id)
            if not table: continue
            type_idx = [th.text.strip() for th in table.find_all('th')].index("性質") if "性質" in [th.text.strip() for th in table.find_all('th')] else -1
            for tr in table.select("tbody tr"):
                tds = tr.find_all('td')
                if len(tds) >= 8:
                    res = re.search(r'(\d{4})', tds[1].text)
                    if res:
                        sid, m_type = res.group(1), "常會"
                        if type_idx != -1 and type_idx < len(tds) and "臨時" in tds[type_idx].text: m_type = "臨時會"
                        if sid not in sg_data: sg_data[sid] = {}
                        if m_type not in sg_data[sid]: sg_data[sid][m_type] = {'condition': '', 'item': '無'}
                        sg_data[sid][m_type]['item'] = tds[7].text.strip()
                        if len(tds) >= 13: sg_data[sid][m_type]['condition'] = f"{tds[11].text.strip()} {tds[12].text.strip()}".strip()
        print(f"   ✅ 零股寶基準資料完備，共 {len(sg_data)} 檔")
    except: pass
    return sg_data

# =========================================================
# Step 2：巡邏 MOPS 總表 (聯集擴充版)
# =========================================================
async def check_mops_summary_async(session, conn, sg_data, history_sids, twse_prices, holidays_set):
    print("\n[Step 2] 🕵️ 巡邏 MOPS 總表...")
    needs = []
    cursor = conn.cursor()
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t108sb31"
    referer = "https://mopsov.twse.com.tw/mops/web/t108sb31_q1"
    
    for q in get_mops_query_years():
        for mk in ['sii', 'otc', 'rotc', 'pub']:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            payload = {"encodeURIComponent": "1", "step": "1", "firstin": "true", "TYPEK": mk, "YEAR": str(q["year"]), "MONTH": q["month"]}
            try:
                html = await fetch_html(session, url, method="POST", payload=payload, referer=referer)
                soup = BeautifulSoup(html, 'html.parser')
                rows = soup.select("table.hasBorder tr")
                print(f"   🔎 查詢 {q['year']}年 / {mk}市場 -> 取得 {max(0, len(rows)-1)} 筆公告")
                
                for tr in rows:
                    tds = tr.find_all('td')
                    if len(tds) < 20: continue 
                    sid, name, m_date = tds[0].text.strip(), tds[1].text.strip(), tds[4].text.strip()
                    m_type = "常會" if "常會" in tds[3].text else "臨時會"
                    full_time = f"{tds[18].text.strip()} {tds[19].text.strip()}" 
                    
                    # 🌟 核心修正： info ∪ suggest ∪ historydb
                    # 只要不存在於零股寶，也不存在於歷史資料庫，才跳過
                    if sid not in sg_data and sid not in history_sids: 
                        continue
                    
                    # 安全地取得零股寶資料 (如果只有歷史庫有，這裡會拿到預設的空字典)
                    sg_info = sg_data.get(sid, {}).get(m_type, {})
                    sg_item = sg_info.get('item', '無')
                    sg_status = "待公布" if sg_item in ["無", ""] or "尚未" in sg_item else "發放"
                    
                    state_key = f"{sid}_{m_type}"
                    last_buy = calculate_last_buy_date(m_date, m_type, holidays_set)
                    price = twse_prices.get(sid, '')
                    
                    cursor.execute("SELECT time, pending FROM mops_states WHERE state_key = ?", (state_key,))
                    row = cursor.fetchone()
                    
                    if full_time != (row[0] if row else "") or (row[1] if row else 0) == 1:
                        cursor.execute("INSERT OR REPLACE INTO mops_states (state_key, time, pending) VALUES (?, ?, 1)", (state_key, full_time))
                        needs.append((sid, m_type, state_key, name, m_date, sg_status, sg_item, sg_info.get('condition', ''), price, last_buy, mk))
                    else:
                        cursor.execute("INSERT OR IGNORE INTO page1_data (stock_id, meeting_type, stock_name, meeting_date, sg_status, sg_item, condition, price, last_buy, needs_debug) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)", (sid, m_type, name, m_date, sg_status, sg_item, sg_info.get('condition', ''), price, last_buy))
                        cursor.execute("UPDATE page1_data SET price=?, last_buy=? WHERE stock_id=? AND meeting_type=?", (price, last_buy, sid, m_type))
                conn.commit()
            except Exception as e: 
                pass
            
    print(f"   ✅ 過濾完成，共有 {len(needs)} 檔異動需進入詳細比對。")
    return needs

# =========================================================
# Step 3：終極完美修復版 (雙元年兼容 + 精準參數提取 + mk 解構)
# =========================================================
async def fetch_st3_detail_async(session, conn, task_data, current_idx, total_count):
    # 🌟 確保解構 11 個變數 (包含從 Step 2 傳過來的 mk)
    sid, m_type, state_key, name, m_date, sg_status, sg_item, cond, price, last_buy, mk = task_data
    
    print(f"   [{current_idx}/{total_count}] 🔍 正在查詢個股: {sid} ({name}) ...")
    
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t108sb16"
    referer = "https://mopsov.twse.com.tw/mops/web/t108sb16_q1"
    cursor = conn.cursor()
    
    # 🌟 設定雙元年變數 (民國年與西元年)
    current_west_year = str(datetime.now().year)        # 例如 "2026"
    current_roc_year = str(datetime.now().year - 1911)  # 例如 "115"
    
    # 第一層：取得清單
    list_payload = {
        "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", 
        "queryName": "co_id", "inpuType": "co_id", "TYPEK": mk, 
        "isnew": "true", "co_id": sid
    }
    
    try:
        html = await fetch_html(session, url, method="POST", payload=list_payload, referer=referer)
        if "安全性考量" in html: return "WAF_BLOCKED"

        soup = BeautifulSoup(html, 'html.parser')
        
        # 決定要從哪個 Form 找 (常會 fm, 臨時會 fm1)
        form_name = "fm" if "常會" in m_type else "fm1"
        target_form = soup.find('form', {'name': form_name})
        
        if not target_form:
            print(f"      ⏳ [{sid}] 找不到 {m_type} 表單 (可能尚無公告)")
            # 沒表單也寫入基礎資訊，但「絕對不要」更新 pending，讓它明天繼續查
            cursor.execute('''INSERT OR REPLACE INTO page1_data (stock_id, meeting_type, stock_name, meeting_date, sg_status, sg_item, condition, mops_status, mops_item, update_date, price, last_buy, needs_debug) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''', (sid, m_type, name, m_date, sg_status, sg_item, cond, "待公布", "開會前另行公告", datetime.now().strftime("%Y/%m/%d"), price, last_buy))
            conn.commit()
            return "SUCCESS_EMPTY"

        rows = target_form.select("table.hasBorder tr")
        announcements = []
        
        for tr in rows:
            tds = tr.find_all('td')
            if len(tds) < 5: continue
            
            row_date = tds[0].text.strip() # 如 115/03/11 或 114/12/31
            subject = tds[2].text.strip()
            
            if "撤銷" in subject: continue
            
            # 🌟 終極年份判定：日期或主旨包含今年(民國/西元皆可)
            is_target_year = (
                current_roc_year in row_date or 
                current_roc_year in subject or 
                current_west_year in subject
            )
            
            if is_target_year and (m_type in subject):
                # 從 onclick 腳本提取後端真正的日期與序號
                btn = tr.find("input", {"type": "button", "value": "詳細資料"})
                if btn and btn.has_attr('onclick'):
                    script = btn['onclick']
                    date1_match = re.search(r'DATE1\.value="(\d+)"', script)
                    seq_no_match = re.search(r'SEQ_NO\.value="(\d+)"', script)
                    
                    if date1_match and seq_no_match:
                        announcements.append({
                            "display_date": row_date,
                            "query_date": date1_match.group(1), # 真實西元日期
                            "seq_no": seq_no_match.group(1)
                        })

        if not announcements:
            print(f"      ⏳ [{sid}] 清單中查無 {current_roc_year} 年公告")
            cursor.execute('''INSERT OR REPLACE INTO page1_data (stock_id, meeting_type, stock_name, meeting_date, sg_status, sg_item, condition, mops_status, mops_item, update_date, price, last_buy, needs_debug) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''', (sid, m_type, name, m_date, sg_status, sg_item, cond, "待公布", "開會前另行公告", datetime.now().strftime("%Y/%m/%d"), price, last_buy))
            conn.commit()
            return "SUCCESS_EMPTY"

        # 排序並抓取最新的一筆詳細內容
        announcements.sort(key=lambda x: x["display_date"])
        target = announcements[-1]

        # 第二層：請求詳細視窗
        detail_payload = {
            "encodeURIComponent": "1",
            "step": "2",
            "firstin": "1",
            "TYPEK": mk,
            "DATE1": target["query_date"],
            "SEQ_NO": target["seq_no"],
            "COMP": sid,
            "SKIND": "A" if "常會" in m_type else "B"
        }
        
        detail_html = await fetch_html(session, url, method="POST", payload=detail_payload, referer=referer)
        
        if "安全性考量" in detail_html: return "WAF_BLOCKED"
        if "代號輸入錯誤" in detail_html:
            print(f"      ❌ [{sid}] 詳細內容請求失敗 (CODE_ERROR)")
            return "ERROR"
            
        detail_text = BeautifulSoup(detail_html, 'html.parser').get_text(separator=' ', strip=True)
        mops_status, mops_item = analyze_souvenir_status(detail_text)
        
        print(f"      📝 [{sid}] 成功解析：{mops_status} - {mops_item}")

        # 寫入 Page 1
        cursor.execute('''
            INSERT OR REPLACE INTO page1_data 
            (stock_id, meeting_type, stock_name, meeting_date, sg_status, sg_item, condition, mops_status, mops_item, update_date, price, last_buy, needs_debug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (sid, m_type, name, m_date, sg_status, sg_item, cond, mops_status, mops_item, datetime.now().strftime("%Y/%m/%d"), price, last_buy))
        
        # 更新 History (僅限常會)
        if "常會" in m_type:
            cursor.execute('''
                INSERT OR REPLACE INTO stock_history 
                (stock_id, year, status, first_status, latest_status, item)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (sid, current_west_year, mops_status, mops_status, mops_status, mops_item))
            
        # ✅ 成功後解除 Pending
        cursor.execute("UPDATE mops_states SET pending = 0 WHERE state_key = ?", (state_key,))
        conn.commit()
        return "SUCCESS"

    except Exception as e:
        print(f"      ❌ [{sid}] 異常: {e}")
        return "ERROR"

# =========================================================
# 取得全新 Session 的輔助函數
# =========================================================
async def create_mops_session():
    session = aiohttp.ClientSession()
    try:
        async with session.get("https://mopsov.twse.com.tw/mops/web/t108sb31_q1", headers={'User-Agent': USER_AGENT}, ssl=False) as resp: await resp.text()
        await asyncio.sleep(1.5)
        async with session.get("https://mopsov.twse.com.tw/mops/web/t108sb16_q1", headers={'User-Agent': USER_AGENT}, ssl=False) as resp: await resp.text()
        await asyncio.sleep(1)
    except: pass
    return session

# =========================================================
# 主程序
# =========================================================
async def main():
    print("=== 🚀 零股寶 & MOPS SQLite 爬蟲系統 (智能隱形版) 啟動 ===")
    conn = init_db()

    # 🌟 讀取歷史資料庫，建立歷史股票代號集合 (Set)，加速比對
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT stock_id FROM stock_history")
    history_sids = {row[0] for row in cursor.fetchall()}
    print(f"[Init] 📚 載入歷史資料庫完畢，共 {len(history_sids)} 檔曾有發放紀錄。")

    session = await create_mops_session()

    taiwan_holidays, twse_prices, sg_data = await asyncio.gather(
        fetch_taiwan_holidays(session), 
        fetch_twse_prices(session), 
        get_sg_data_async(session)
    )
    
    # 🌟 將 history_sids 傳入 Step 2
    needs = await check_mops_summary_async(session, conn, sg_data, history_sids, twse_prices, holidays_set=taiwan_holidays)
    
    if needs:
        total_needs = len(needs)
        print(f"\n[Step 3] 🎯 開始處理 {total_needs} 檔異動股票... (已開啟循序防 Ban 機制，請耐心等待)")
        
        for i, task_data in enumerate(needs, 1):
            if i > 1 and i % 50 == 0:
                print(f"\n   🔄 [進度 {i}/{total_needs}] 安全機制啟動，深呼吸換氣中 (更新 Cookie)...\n")
                await session.close()
                await asyncio.sleep(5)
                session = await create_mops_session()
            
            while True:
                result = await fetch_st3_detail_async(session, conn, task_data, i, total_needs)
                
                if result == "WAF_BLOCKED":
                    print(f"      🛑 [防護啟動] 遭防火牆攔截！強制休眠 60 秒後換 IP 重試 [{task_data[0]}]...")
                    await asyncio.sleep(60)
                    await session.close()
                    session = await create_mops_session()
                    continue 
                
                break 
            
            await asyncio.sleep(random.uniform(1.0, 2.5))
            
    else:
        print("\n✅ 今日無新公告。")
        
    # === 將 SQLite 資料庫轉存為 JSON 供前端使用 ===
    print("\n📦 正在將資料庫匯出為前端 JSON 檔案...")
    
    cursor.execute("SELECT * FROM page1_data")
    columns = [desc[0] for desc in cursor.description]
    page1_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    
    with open('data/page1_data.json', 'w', encoding='utf-8') as f:
        json.dump(page1_rows, f, ensure_ascii=False, indent=2)

    cursor.execute("SELECT * FROM stock_history")
    columns = [desc[0] for desc in cursor.description]
    history_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    
    with open('data/stock_history.json', 'w', encoding='utf-8') as f:
        json.dump(history_rows, f, ensure_ascii=False, indent=2)
        
    print("✨ JSON 匯出完成！前端網頁可以更新了！")
        
    await session.close()
    conn.close()
    print("✨ 所有任務完成，資料庫已安全斷線！")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n⚠️ 程式已被手動中斷。")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n⚠️ 程式已被手動中斷。")
