import asyncio
import json
import re
import random
import os
import urllib.request
import ssl
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from collections import deque

# =========================================================
# 系統設定
# =========================================================
# 檔案儲存路徑設定 (存放在 data/ 資料夾下，方便 GitHub Pages 讀取)
DB_HISTORY_FILE = 'data/db_history.json'    
DB_PAGE1_FILE = 'data/db_page1.json'        
STATE_FILE = 'data/state_mops_dates.json'   
MAX_RETRIES = 3
FAIL_THRESHOLD = 3 

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# =========================================================
# 模組 1：股價與最後買進日演算法 (含台灣假日系統與 SSL 繞過)
# =========================================================

def fetch_taiwan_holidays():
    """獲取台灣行政機關辦公日曆表，建立假日集合"""
    print("\n[Init] 📅 正在獲取台灣政府行政機關日曆表...")
    holidays = set()
    try:
        # 建立略過 SSL 驗證的環境，避免憑證錯誤
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = "https://data.ntpc.gov.tw/api/datasets/308DCD75-6434-45BC-A950-5BEF4ADC36EA/json?page=0&size=1000"
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            data = json.loads(response.read().decode())
            for item in data:
                if item.get('isHoliday') == '是':
                    date_str = item.get('date') 
                    if date_str:
                        date_str = date_str.replace('/', '')
                        holidays.add(date_str)
        print(f"   ✅ 成功獲取假日資料，共 {len(holidays)} 天假期")
    except Exception as e:
        print(f"   ⚠️ 假日資料獲取失敗，退回基本週休二日計算: {e}")
    return holidays

def fetch_twse_prices():
    """從證交所與櫃買中心 OpenAPI 獲取最新收盤價快取字典"""
    print("[Init] 📈 正在從證交所獲取最新收盤價...")
    price_dict = {}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # 抓取上市股價
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        req = urllib.request.Request(url_twse, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            data = json.loads(response.read().decode())
            for item in data: price_dict[item['Code']] = item['ClosingPrice']
                
        # 抓取上櫃股價
        url_otc = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
        req_otc = urllib.request.Request(url_otc, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req_otc, timeout=10, context=ctx) as response:
            data_otc = json.loads(response.read().decode())
            for item in data_otc: price_dict[item['SecuritiesCompanyCode']] = item['Close']
                
        print(f"   ✅ 成功獲取上市櫃股價，共 {len(price_dict)} 檔")
    except Exception as e: print(f"   ⚠️ 股價獲取失敗: {e}")
    return price_dict

def calculate_last_buy_date(meeting_date_str, meeting_type, holidays_set):
    """精準推算最後買進日，排除台灣假日，並輸出包含星期的格式 (例如: 2026/04/23 (四))"""
    if not meeting_date_str: return ""
    try:
        parts = meeting_date_str.split('/')
        roc_year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        meeting_date = datetime(roc_year + 1911, month, day)
        
        # 法規：常會前60日停止過戶，臨時會前30日
        stop_days = 60 if "常會" in meeting_type else 30
        stop_start_date = meeting_date - timedelta(days=stop_days - 1)
        last_transfer_date = stop_start_date - timedelta(days=1)
        
        working_days_to_subtract = 2
        last_buy = last_transfer_date
        
        # 迴圈往前推算，避開週末與國定假日
        while working_days_to_subtract > 0:
            last_buy -= timedelta(days=1)
            date_str_key = last_buy.strftime("%Y%m%d")
            # 5:週六, 6:週日
            if last_buy.weekday() >= 5 or date_str_key in holidays_set: continue
            working_days_to_subtract -= 1
                
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return last_buy.strftime("%Y/%m/%d") + f" ({weekdays[last_buy.weekday()]})"
    except: return ""

# =========================================================
# 模組 2：字串分析與儲存工具
# =========================================================

def analyze_souvenir_status(body_text: str):
    if not body_text or len(body_text.strip()) < 10:
        return ("無資料", "網頁未載入")
    text = re.sub(r'[\s\n\r　]+', ' ', body_text)
    flat_text = text.replace(' ', '')
    if "尚未決定是否發放紀念品" in flat_text or "開會55日前再行公告" in flat_text:
        return ("待公布", "開會前另行公告")
    if "未發放紀念品" in flat_text or "不發放紀念品" in flat_text or "本年度未發放紀念品" in flat_text:
        return ("不發放", "不發放")
    match = re.search(r"紀念品(?:名稱)?(?:為)?[:：]\s*(.*?)(?:[。，；\.]|$)", text)
    if match:
        item = match.group(1).strip()
        for d in ["(一)", "(二)", "發放原則", "持股", "股東", "本公司", "1."]: item = item.split(d)[0]
        return ("發放", item.strip())
    if "發放" in flat_text and "紀念品" in flat_text:
        for line in body_text.splitlines():
            if "紀念品為" in line or "紀念品：" in line or "紀念品:" in line or "發放原則" in line:
                clean = line.strip()
                for d in ["(一)", "發放原則", "持股", "股東"]: clean = clean.split(d)[0]
                return ("發放", clean.strip())
        return ("發放", "有發放但未載明品項")
    return ("無資料", "查無紀念品段落")

def infer_sg_status(item_text: str):
    if not item_text or item_text == "無" or "尚未" in item_text or "近期決定" in item_text: return "待公布"
    return "發放"

def save_all_data(db_h, db_p1, states):
    os.makedirs('data', exist_ok=True)
    with open(DB_HISTORY_FILE, 'w', encoding='utf-8') as f: json.dump(db_h, f, ensure_ascii=False, indent=2)
    with open(DB_PAGE1_FILE, 'w', encoding='utf-8') as f: json.dump(db_p1, f, ensure_ascii=False, indent=2)
    with open(STATE_FILE, 'w', encoding='utf-8') as f: json.dump(states, f, ensure_ascii=False, indent=2)

def get_mops_query_years():
    roc_year = datetime.now().year - 1911
    month = datetime.now().month
    queries = []
    if 1 <= month <= 2: queries.extend([{"year": roc_year - 1, "month": "11"}, {"year": roc_year - 1, "month": "12"}, {"year": roc_year, "month": "all"}])
    elif 3 <= month <= 10: queries.append({"year": roc_year, "month": "all"})
    else: queries.extend([{"year": roc_year, "month": "all"}, {"year": roc_year + 1, "month": "all"}])
    return queries

# =========================================================
# 爬蟲階段：Step 1, Step 2, Step 3
# =========================================================

async def get_sg_data(page: Page):
    """獲取零股寶聯集基準資料 (主要抓取發放條件)"""
    print("\n[Step 1] 🚀 獲取零股寶聯集基準資料...")
    sg_data = {}
    try:
        await page.goto("https://stockgift.tw/STOCK/Suggest/Suggest", timeout=60000)
        await page.evaluate("document.querySelector('.datatable-selector').value='2500'; document.querySelector('.datatable-selector').dispatchEvent(new Event('change'));")
        await asyncio.sleep(3)
        soup = BeautifulSoup(await page.content(), 'html.parser')
        for tr in soup.select("#datatables tbody tr"):
            tds = tr.find_all('td')
            if len(tds) >= 7:
                res = re.search(r'\((\d+)\)', tds[0].text)
                if res:
                    sid = res.group(1)
                    if sid not in sg_data: sg_data[sid] = {}
                    sg_data[sid]['常會'] = {'item': '無', 'condition': f"{tds[4].text.strip()} {tds[6].text.strip()}".strip()}
    except Exception as e: print(f"⚠️ Suggest 錯誤: {e}")
    
    try:
        await page.goto("https://stockgift.tw/STOCK/Stock/Info", timeout=60000)
        await page.evaluate("document.querySelectorAll('.datatable-selector').forEach(s=>{s.value='2500'; s.dispatchEvent(new Event('change'));})")
        await asyncio.sleep(3)
        soup = BeautifulSoup(await page.content(), 'html.parser')
        for table_id in ["hadEntrustdatatable", "hadEntrustdatatable2"]:
            table = soup.find(id=table_id)
            if not table: continue
            headers = [th.text.strip() for th in table.find_all('th')]
            try: type_idx = headers.index("性質")
            except: type_idx = -1
            for tr in table.select("tbody tr"):
                tds = tr.find_all('td')
                if len(tds) >= 8:
                    res = re.search(r'(\d{4})', tds[1].text)
                    if res: 
                        sid = res.group(1)
                        m_type = "常會"
                        if type_idx != -1 and type_idx < len(tds) and "臨時" in tds[type_idx].text: m_type = "臨時會"
                        if sid not in sg_data: sg_data[sid] = {}
                        if m_type not in sg_data[sid]: sg_data[sid][m_type] = {'condition': '', 'item': '無'}
                        sg_data[sid][m_type]['item'] = tds[7].text.strip()
                        if len(tds) >= 13:
                            cond = f"{tds[11].text.strip()} {tds[12].text.strip()}".strip()
                            if cond: sg_data[sid][m_type]['condition'] = cond
    except Exception as e: print(f"⚠️ Info 錯誤: {e}")
    print(f"   ✅ 零股寶基準資料完備，聯集共 {len(sg_data)} 檔")
    return sg_data

async def check_mops_summary(page: Page, state_dates: dict, db_page1: list, sg_data: dict, db_history: list, twse_prices: dict, holidays_set: set):
    """巡邏 MOPS 總表，更新股價與買進日，並挑出需要詳細爬取的清單"""
    print("\n[Step 2] 🕵️ 巡邏 MOPS 總表並更新股價/日期...")
    needs = []
    for sid, val in state_dates.items():
        if isinstance(val, str): state_dates[sid] = {"time": val, "pending": False}
    
    db_page1_updated = False 
    
    await page.goto("https://mopsov.twse.com.tw/mops/web/t108sb31_q1", timeout=60000)
    for q in get_mops_query_years():
        for mk in ['sii', 'otc', 'rotc', 'pub']:
            try:
                type_sel = page.locator("select[name='TYPEK']")
                if await type_sel.count() > 0: await type_sel.first.select_option(mk)
                else: await page.locator(f"input[name='TYPEK'][value='{mk}']").first.click()
                await page.locator("input#YEAR").first.fill(str(q["year"]))
                await page.locator("select#MONTH").first.select_option(q["month"])
                await page.locator("input[type='button'][value*='查詢']").first.click()
                print(f"   🔎 查詢 {q['year']} 年 / {q['month']} 月 / {mk} 市場...")
                try: await page.wait_for_selector("table.hasBorder", timeout=8000)
                except: continue 
                
                soup = BeautifulSoup(await page.content(), 'html.parser')
                for tr in soup.select("table.hasBorder tr.even, table.hasBorder tr.odd"):
                    tds = tr.find_all('td')
                    if len(tds) < 20: continue
                    sid, name, m_type, m_date = tds[0].text.strip(), tds[1].text.strip(), ("常會" if "常會" in tds[3].text else "臨時會"), tds[4].text.strip()
                    full_time = f"{tds[18].text.strip()} {tds[19].text.strip()}" 
                    
                    if sid not in sg_data: continue
                    sg_info = sg_data[sid].get(m_type, {})
                    sg_item = sg_info.get('item', '無')
                    sg_status = infer_sg_status(sg_item)
                    
                    # 計算最新股價與買進日
                    calculated_last_buy = calculate_last_buy_date(m_date, m_type, holidays_set)
                    current_price = twse_prices.get(sid, '')
                    
                    p1_idx = next((i for i, x in enumerate(db_page1) if x['stock_id'] == sid and x['meeting_type'] == m_type), None)
                    if p1_idx is None:
                        db_page1.append({
                            'stock_id': sid, 'stock_name': name, 'meeting_date': m_date, 'meeting_type': m_type,
                            'sg_status': sg_status, 'sg_item': sg_item, 'condition': sg_info.get('condition', ''),
                            'mops_status': '', 'mops_item': '', 'update_date': '',
                            'price': current_price, 'last_buy': calculated_last_buy, 'needs_debug': False
                        })
                        db_page1_updated = True
                    else:
                        # 強制更新該檔股票的股價與日期
                        db_page1[p1_idx].update({
                            'stock_name': name, 'meeting_date': m_date, 'sg_status': sg_status, 'sg_item': sg_item, 
                            'condition': sg_info.get('condition', ''), 'price': current_price, 'last_buy': calculated_last_buy
                        })
                        db_page1_updated = True
                    
                    state_key = f"{sid}_{m_type}"
                    current_state = state_dates.get(state_key, {"time": "", "pending": False})
                    
                    if full_time != current_state.get("time") or current_state.get("pending") is True:
                        state_dates[state_key] = {"time": full_time, "pending": True}
                        db_page1[p1_idx].update({'mops_status': '', 'mops_item': '', 'needs_debug': False})
                        needs.append((sid, m_type, state_key))
            except: pass
            
    if db_page1_updated or needs:
        save_all_data(db_history, db_page1, state_dates)
        
    print(f"   ✅ 過濾與標記完成，共有 {len(needs)} 檔異動需進入詳細比對。")
    return needs, db_page1

async def fetch_st3_detail(sid, m_type, state_key, page, db_h, db_p1, states, shared_state):
    """深入爬取 MOPS 彈出視窗內的詳細紀念品資訊"""
    await asyncio.sleep(random.uniform(0.5, 1.25) + shared_state["consecutive_bans"] * 4.0)
    if state_key not in states: states[state_key] = {"time": "", "pending": True, "fail_count": 0}
    if "fail_count" not in states[state_key]: states[state_key]["fail_count"] = 0

    try:
        await page.goto("https://mopsov.twse.com.tw/mops/web/t108sb16_q1", wait_until="domcontentloaded", timeout=0)
        sel = page.locator("select#isnew")
        if await sel.count() > 0: await sel.first.select_option(value="false")
        await page.wait_for_selector("input#co_id", state="visible", timeout=10000)
        await page.locator("input#co_id").first.fill(sid)
        await page.locator("input[type='button'][value*='查詢']").first.click()
        await page.wait_for_selector("#table01", state="attached", timeout=15000)

        form_name = "fm" if "常會" in m_type else "fm1"
        rows = page.locator(f"form[name='{form_name}'] table.hasBorder tr")
        if await rows.count() <= 1: rows = page.locator("table.hasBorder tr")
        current_roc_year = str(datetime.now().year - 1911)
        year_rows = []
        
        if await rows.count() > 1:
            for i in range(1, await rows.count()):
                row_locator = rows.nth(i)
                tds = row_locator.locator("td")
                if await tds.count() < 4: continue 
                row_text = await row_locator.inner_text()
                if "撤銷" in row_text: continue
                date_text = await tds.nth(0).inner_text()
                subject_text = await tds.nth(2).inner_text()
                if date_text.strip().startswith(current_roc_year) or current_roc_year in subject_text:
                    btn = row_locator.locator("input[type='button'][value*='詳細']").first
                    if await btn.count() > 0: year_rows.append((date_text.strip(), btn))

        if not year_rows:
            states[state_key]["fail_count"] += 1
            if states[state_key]["fail_count"] >= FAIL_THRESHOLD:
                for p in db_p1:
                    if p['stock_id'] == sid and p['meeting_type'] == m_type:
                        p.update({'mops_status': "⚠️ 異常", 'mops_item': "多次查無按鈕，請手動 Debug", 'update_date': datetime.now().strftime("%Y/%m/%d"), 'needs_debug': True})
            states[state_key]["pending"] = False
            save_all_data(db_h, db_p1, states)
            shared_state["consecutive_bans"] = 0
            return "RETRY_LATER"

        year_rows.sort(key=lambda x: x[0])
        first_btn, latest_btn = year_rows[0][1], year_rows[-1][1]

        async def read_popup_content(btn_locator):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            async with page.expect_popup(timeout=15000) as popup_info: await btn_locator.click()
            popup = await popup_info.value
            try:
                await popup.wait_for_load_state("load", timeout=20000)
                try: await popup.wait_for_function("() => document.body.innerText.length > 50", timeout=10000)
                except: pass
                await asyncio.sleep(1.0)
                return analyze_souvenir_status(await popup.locator("body").inner_text())
            finally: await popup.close()

        first_s, first_i = await read_popup_content(first_btn)
        latest_s, latest_i = (first_s, first_i) if len(year_rows) == 1 else await read_popup_content(latest_btn)
            
        for p in db_p1:
            if p['stock_id'] == sid and p['meeting_type'] == m_type:
                p.update({'mops_status': latest_s, 'mops_item': latest_i, 'update_date': datetime.now().strftime("%Y/%m/%d")})
        
        if "常會" in m_type:
            for h in db_h:
                if h['stock_id'] == sid:
                    y_str = str(datetime.now().year)
                    rec = h.setdefault('history', {}).setdefault(y_str, {})
                    old_s = rec.get('status', '待公布')
                    if latest_s == '發放': rec['status'] = '發放' if old_s in ['待公布', '無公告', '無資訊'] else (old_s if '發放' in old_s else f"{old_s} -> 發放")
                    elif latest_s == '不發放': rec['status'] = '不發放' if old_s in ['待公布', '無公告', '無資訊'] else (old_s if '不發放' in old_s else f"{old_s} -> 不發放")
                    elif first_s != latest_s: rec['status'] = f"{'待公布' if first_s in ['無資料', '無資訊'] else first_s} -> {latest_s}"
                    else: rec['status'] = latest_s
                    rec['item'] = latest_i
                    print(f"   🎯 [{sid}] 更新成功: {rec['status']} - {latest_i}")

        states[state_key].update({"pending": False, "fail_count": 0})
        save_all_data(db_h, db_p1, states)
        shared_state["consecutive_bans"] = 0
        return "SUCCESS"

    except PlaywrightTimeoutError: shared_state["consecutive_bans"] += 1; raise
    except Exception: return "ERROR"

# =========================================================
# 主程序執行入口
# =========================================================

async def main():
    if not os.path.exists(DB_HISTORY_FILE): print("❌ 缺少 DB_HISTORY"); return
    with open(DB_HISTORY_FILE, 'r', encoding='utf-8') as f: db_h = json.load(f)
    db_p1 = json.load(open(DB_PAGE1_FILE,'r',encoding='utf-8')) if os.path.exists(DB_PAGE1_FILE) else []
    try: states = json.load(open(STATE_FILE,'r',encoding='utf-8'))
    except: states = {}
    shared_state = {"consecutive_bans": 0}

    # 🔥 1. 抓取政府假日資料
    taiwan_holidays = fetch_taiwan_holidays()
    # 🔥 2. 抓取官方股價快取
    twse_prices = fetch_twse_prices()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) 
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        
        # 爬取零股寶
        sg_data = await get_sg_data(page)
        
        # 🔥 3. 傳入假日集合與股價進行比對，並強制更新股價與買進日
        needs, db_p1 = await check_mops_summary(page, states, db_p1, sg_data, db_h, twse_prices, taiwan_holidays)
        
        if needs:
            print(f"\n[Step 3] 🎯 開始處理 {len(needs)} 檔異動股票...")
            queue = deque(needs)
            retried_sids = set() 
            while queue:
                sid, m_type, state_key = queue.popleft()
                success_flag = False
                for retry in range(MAX_RETRIES):
                    try:
                        result = await fetch_st3_detail(sid, m_type, state_key, page, db_h, db_p1, states, shared_state)
                        if result == "RETRY_LATER":
                            if state_key not in retried_sids: queue.append((sid, m_type, state_key)); retried_sids.add(state_key)
                            success_flag = True; break
                        elif result == "SUCCESS": success_flag = True; break
                        elif result == "ERROR": break
                    except PlaywrightTimeoutError:
                        bans = shared_state["consecutive_bans"]
                        print(f"   ❌ [{sid}] Timeout。連續: {bans}")
                        await asyncio.sleep(min(random.uniform(30.0, 45.0) * (1.5 ** (bans - 1)), 1200.0))
                if not success_flag: print(f"   ❌ [{sid}] 本輪失敗。")
        else: print("\n✅ 今日無新公告。")
        await browser.close()

if __name__ == "__main__": asyncio.run(main())
