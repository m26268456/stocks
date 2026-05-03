import asyncio
import json
import re
import random
import os
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from collections import deque

# =========================================================
# 系統設定
# =========================================================
DB_HISTORY_FILE = 'db_history.json'    
DB_PAGE1_FILE = 'db_page1.json'        
STATE_FILE = 'state_mops_dates.json'   
MAX_RETRIES = 3

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# =========================================================
# 工具函式
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
        for d in ["(一)", "(二)", "發放原則", "持股", "股東", "本公司", "1."]:
            item = item.split(d)[0]
        return ("發放", item.strip())
        
    if "發放" in flat_text and "紀念品" in flat_text:
        for line in body_text.splitlines():
            if "紀念品為" in line or "紀念品：" in line or "紀念品:" in line or "發放原則" in line:
                clean = line.strip()
                for d in ["(一)", "發放原則", "持股", "股東"]:
                    clean = clean.split(d)[0]
                return ("發放", clean.strip())
        return ("發放", "有發放但未載明品項")
        
    return ("無資料", "查無紀念品段落")

def infer_sg_status(item_text: str):
    if not item_text or item_text == "無" or "尚未" in item_text or "近期決定" in item_text:
        return "待公布"
    return "發放"

def save_all_data(db_h, db_p1, states):
    with open(DB_HISTORY_FILE, 'w', encoding='utf-8') as f: json.dump(db_h, f, ensure_ascii=False, indent=2)
    with open(DB_PAGE1_FILE, 'w', encoding='utf-8') as f: json.dump(db_p1, f, ensure_ascii=False, indent=2)
    with open(STATE_FILE, 'w', encoding='utf-8') as f: json.dump(states, f, ensure_ascii=False, indent=2)

def get_mops_query_years():
    today = datetime.now()
    roc_year = today.year - 1911
    month = today.month
    queries = []
    if 1 <= month <= 2:
        queries.append({"year": roc_year - 1, "month": "11"})
        queries.append({"year": roc_year - 1, "month": "12"})
        queries.append({"year": roc_year, "month": "all"})
    elif 3 <= month <= 10:
        queries.append({"year": roc_year, "month": "all"})
    else:
        queries.append({"year": roc_year, "month": "all"})
        queries.append({"year": roc_year + 1, "month": "all"})
    return queries

# =========================================================
# 爬蟲階段
# =========================================================

async def get_sg_data(page: Page):
    """Step 1 & 1.5: 建立 零股寶聯集 (Suggest U Info) 的基準資料"""
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
                    cond = f"{tds[4].text.strip()} {tds[6].text.strip()}".strip()
                    if sid not in sg_data: sg_data[sid] = {}
                    # Suggest 預設先存為常會
                    sg_data[sid]['常會'] = {'item': '無', 'condition': cond}
    except Exception as e: print(f"⚠️ Suggest 錯誤: {e}")
    
    try:
        await page.goto("https://stockgift.tw/STOCK/Stock/Info", timeout=60000)
        await page.evaluate("document.querySelectorAll('.datatable-selector').forEach(s=>{s.value='2500'; s.dispatchEvent(new Event('change'));})")
        await asyncio.sleep(3)
        soup = BeautifulSoup(await page.content(), 'html.parser')
        
        # 分別抓取 hadEntrustdatatable 與 hadEntrustdatatable2 兩個表格
        for table_id in ["hadEntrustdatatable", "hadEntrustdatatable2"]:
            table = soup.find(id=table_id)
            if not table: continue
            
            # 動態尋找「性質」欄位的位置
            headers = [th.text.strip() for th in table.find_all('th')]
            try: type_idx = headers.index("性質")
            except ValueError: type_idx = -1
            
            for tr in table.select("tbody tr"):
                tds = tr.find_all('td')
                if len(tds) >= 8:
                    res = re.search(r'(\d{4})', tds[1].text)
                    if res: 
                        sid = res.group(1)
                        
                        # 判斷是常會或臨時會
                        m_type = "常會"
                        if type_idx != -1 and type_idx < len(tds):
                            if "臨時" in tds[type_idx].text:
                                m_type = "臨時會"
                                
                        if sid not in sg_data: sg_data[sid] = {}
                        if m_type not in sg_data[sid]: sg_data[sid][m_type] = {'condition': '', 'item': '無'}
                        
                        sg_data[sid][m_type]['item'] = tds[7].text.strip()
                        if len(tds) >= 13:
                            cond = f"{tds[11].text.strip()} {tds[12].text.strip()}".strip()
                            if cond: sg_data[sid][m_type]['condition'] = cond
    except Exception as e: print(f"⚠️ Info 錯誤: {e}")
    
    print(f"   ✅ 零股寶基準資料完備，聯集共 {len(sg_data)} 檔")
    return sg_data

async def check_mops_summary(page: Page, state_dates: dict, db_page1: list, sg_data: dict, db_history: list):
    """Step 2: 巡邏 MOPS 總表，更新 State 與 Pending 標記"""
    print("\n[Step 2] 🕵️ 巡邏 MOPS 總表...")
    needs = []
    
    for sid, val in state_dates.items():
        if isinstance(val, str):
            state_dates[sid] = {"time": val, "pending": False}
    
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
                    
                    sid = tds[0].text.strip()
                    name = tds[1].text.strip()
                    
                    m_type = "常會" if "常會" in tds[3].text else "臨時會"
                    m_date = tds[4].text.strip()
                    full_time = f"{tds[18].text.strip()} {tds[19].text.strip()}" 
                    
                    if sid not in sg_data: continue
                    
                    # 依據會議類型 (常會/臨時會) 抓取對應資料
                    sg_info = sg_data[sid].get(m_type, {})
                    sg_item = sg_info.get('item', '無')
                    condition = sg_info.get('condition', '')
                    sg_status = infer_sg_status(sg_item)
                    
                    p1_idx = next((i for i, x in enumerate(db_page1) if x['stock_id'] == sid and x['meeting_type'] == m_type), None)
                    
                    if p1_idx is None:
                        db_page1.append({
                            'stock_id': sid, 'stock_name': name, 'meeting_date': m_date, 'meeting_type': m_type,
                            'sg_status': sg_status, 'sg_item': sg_item, 'condition': condition,
                            'mops_status': '', 'mops_item': '', 'update_date': ''
                        })
                        p1_idx = len(db_page1) - 1
                    else:
                        db_page1[p1_idx]['stock_name'] = name
                        db_page1[p1_idx]['meeting_date'] = m_date
                        db_page1[p1_idx]['sg_status'] = sg_status
                        db_page1[p1_idx]['sg_item'] = sg_item
                        db_page1[p1_idx]['condition'] = condition
                    
                    current_state = state_dates.get(sid, {"time": "", "pending": False})
                    
                    if full_time != current_state.get("time") or current_state.get("pending") is True:
                        state_dates[sid] = {"time": full_time, "pending": True}
                        db_page1[p1_idx]['mops_status'] = ""
                        db_page1[p1_idx]['mops_item'] = ""
                        needs.append((sid, m_type))
                        
            except Exception as e: pass
            
    save_all_data(db_history, db_page1, state_dates)
    print(f"   ✅ 過濾與標記完成，共有 {len(needs)} 檔異動需進入詳細比對。")
    return needs, db_page1

async def fetch_st3_detail(sid, m_type, page, db_h, db_p1, states, shared_state):
    """Step 3: 具備自我修復能力的歷史軌跡爬取 (強化 Popup 渲染等待版 + 失敗重試隊列 + 順序排序防呆)"""
    
    penalty_delay = shared_state["consecutive_bans"] * 4.0
    base_wait = random.uniform(0.5, 1.25) + penalty_delay
    print(f"   ⏳ 準備處理 [{sid}] ({m_type})，行前延遲 {base_wait:.1f} 秒...")
    await asyncio.sleep(base_wait)

    if "fail_count" not in states[sid]:
        states[sid]["fail_count"] = 0

    try:
        await page.goto("https://mopsov.twse.com.tw/mops/web/t108sb16_q1", wait_until="domcontentloaded", timeout=0)
        
        sel = page.locator("select#isnew")
        if await sel.count() > 0:
            await sel.first.select_option(value="false")
            await asyncio.sleep(random.uniform(0.5, 1.0))

        await page.wait_for_selector("input#co_id", state="visible", timeout=10000)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await page.locator("input#co_id").first.fill(sid)
        await asyncio.sleep(random.uniform(1.0, 2.0))

        await page.wait_for_selector("input[type='button'][value*='查詢']", state="visible", timeout=5000)
        await page.locator("input[type='button'][value*='查詢']").first.click()

        await page.wait_for_selector("#table01", state="attached", timeout=15000)
        await asyncio.sleep(2)

        form_name = "fm" if "常會" in m_type else "fm1"
        
        rows = page.locator(f"form[name='{form_name}'] table.hasBorder tr")
        
        # 【防呆機制】如果指定的 form 找不到資料，放寬範圍抓取畫面上所有的資料列表
        if await rows.count() <= 1:
            rows = page.locator("table.hasBorder tr")
        
        current_roc_year = str(datetime.now().year - 1911)
        year_rows = []
        
        if await rows.count() > 1:
            for i in range(1, await rows.count()):
                row_locator = rows.nth(i)
                tds = row_locator.locator("td")
                
                if await tds.count() < 4: 
                    continue # 略過不完整的列
                
                row_text = await row_locator.inner_text()
                if "撤銷" in row_text:
                    continue
                    
                date_text = await tds.nth(0).inner_text()
                subject_text = await tds.nth(2).inner_text()
                
                # 【關鍵修正】：放寬年份判斷！
                # 臨時會經常在「前一年」年底發布公告，因此同步檢查主旨(subject_text)是否包含今年年份
                if date_text.strip().startswith(current_roc_year) or current_roc_year in subject_text:
                    btn = row_locator.locator("input[type='button'][value*='詳細']").first
                    
                    if await btn.count() > 0:
                        year_rows.append((date_text.strip(), btn))

        if not year_rows:
            states[sid]["fail_count"] += 1
            print(f"   ⚠️ [{sid}] {m_type} 找不到任何詳細按鈕 (連續失敗: {states[sid]['fail_count']} 次)")
            
            if states[sid]["fail_count"] >= 3:
                for p in db_p1:
                    if p['stock_id'] == sid and p['meeting_type'] == m_type:
                        p['mops_status'] = "⚠️ 異常"
                        p['mops_item'] = "多次查無按鈕，請手動 Debug"
                        p['update_date'] = datetime.now().strftime("%Y/%m/%d")
                print(f"   🚨 [{sid}] 失敗次數過多，已標記至前端由使用者確認。")
            
            save_all_data(db_h, db_p1, states)
            shared_state["consecutive_bans"] = 0
            return "RETRY_LATER"

        # 依據日期字串排序 (升冪：舊到新)
        year_rows.sort(key=lambda x: x[0])

        # 排序後提取：首筆(最舊)、末筆(最新)
        first_btn = year_rows[0][1]
        latest_btn = year_rows[-1][1]

        async def read_popup_content(btn_locator):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            async with page.expect_popup(timeout=15000) as popup_info:
                await btn_locator.click()
            popup = await popup_info.value
            
            try:
                await popup.wait_for_load_state("load", timeout=20000)
                try:
                    await popup.wait_for_function(
                        "() => document.body.innerText.length > 50", 
                        timeout=10000
                    )
                except PlaywrightTimeoutError:
                    print(f"      [警告] 彈出視窗文字量未達標準，可能網路過慢...")
                await asyncio.sleep(random.uniform(1.0, 2.0))
                body_text = await popup.locator("body").inner_text()
                return analyze_souvenir_status(body_text)
            finally:
                await popup.close()

        first_s, first_i = await read_popup_content(first_btn)

        if len(year_rows) == 1:
            latest_s, latest_i = first_s, first_i
        else:
            latest_s, latest_i = await read_popup_content(latest_btn)
            
        today_str = datetime.now().strftime("%Y/%m/%d")
        
        for p in db_p1:
            if p['stock_id'] == sid and p['meeting_type'] == m_type:
                p['mops_status'] = latest_s
                p['mops_item'] = latest_i
                p['update_date'] = today_str
        
        if "常會" in m_type:
            current_year_str = str(datetime.now().year)
            for h in db_h:
                if h['stock_id'] == sid:
                    if current_year_str not in h['history']: h['history'][current_year_str] = {'status':'待公布','item':'無'}
                    rec = h['history'][current_year_str]
                    
                    if first_s != latest_s:
                        display_first_s = "待公布" if first_s in ["無資料", "無資訊"] else first_s
                        rec['status'] = f"{display_first_s} -> {latest_s}"
                    else:
                        rec['status'] = latest_s
                        
                    rec['item'] = latest_i
                    print(f"   🎯 [{sid}] 常會更新成功: {rec['status']} - {latest_i}")
        else:
            # 補上臨時會的成功提示，臨時會不存入歷史軌跡 (db_h)，但有更新 db_page1
            print(f"   🎯 [{sid}] 臨時會更新成功: {latest_s} - {latest_i}")

        states[sid]["pending"] = False
        states[sid]["fail_count"] = 0
        save_all_data(db_h, db_p1, states)

        states[sid]["pending"] = False
        states[sid]["fail_count"] = 0
        save_all_data(db_h, db_p1, states)
        
        if shared_state["consecutive_bans"] > 0:
            print("   🟢 網路暢通，重置封鎖計數器。")
        shared_state["consecutive_bans"] = 0
        
        return "SUCCESS"

    except PlaywrightTimeoutError:
        shared_state["consecutive_bans"] += 1
        raise PlaywrightTimeoutError("Timeout")
    except Exception as e:
        print(f"   ⚠️ [{sid}] 爬取異常: {e}")
        return "ERROR"

# =========================================================
# 執行程序
# =========================================================

async def main():
    if not os.path.exists(DB_HISTORY_FILE): 
        print("❌ 缺少 DB_HISTORY"); return
    
    with open(DB_HISTORY_FILE, 'r', encoding='utf-8') as f: db_h = json.load(f)
    db_p1 = json.load(open(DB_PAGE1_FILE,'r',encoding='utf-8')) if os.path.exists(DB_PAGE1_FILE) else []
    try: states = json.load(open(STATE_FILE,'r',encoding='utf-8'))
    except: states = {}

    shared_state = {"consecutive_bans": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) 
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        
        sg_data = await get_sg_data(page)
        
        needs, db_p1 = await check_mops_summary(page, states, db_p1, sg_data, db_h)
        
        if needs:
            print(f"\n[Step 3] 🎯 開始處理 {len(needs)} 檔異動股票...")
            queue = deque(needs)
            retried_sids = set() 

            while queue:
                sid, m_type = queue.popleft()
                
                success_flag = False
                for retry in range(MAX_RETRIES):
                    try:
                        result = await fetch_st3_detail(sid, m_type, page, db_h, db_p1, states, shared_state)
                        
                        if result == "RETRY_LATER":
                            if sid not in retried_sids:
                                print(f"   🔄 [{sid}] 尚未準備好，移至處理序列最後面...")
                                queue.append((sid, m_type))
                                retried_sids.add(sid)
                            success_flag = True 
                            break
                        elif result == "SUCCESS":
                            success_flag = True
                            break
                        elif result == "ERROR":
                            break
                    except PlaywrightTimeoutError:
                        bans = shared_state["consecutive_bans"]
                        print(f"   ❌ [{sid}] 處理失敗：偵測到 Timeout (可能被 BAN)。連續次數：{bans}")
                        
                        base_cooldown = random.uniform(30.0, 45.0)
                        multiplier = 1.5 ** (bans - 1)
                        cooldown = min(base_cooldown * multiplier, 1200.0)
                        
                        print(f"   🛡️ 啟動滾動式冷卻：休眠 {cooldown:.1f} 秒...")
                        await asyncio.sleep(cooldown)
                        
                if not success_flag:
                    print(f"   ❌ [{sid}] 本輪處理完畢仍失敗。")
        else:
            print("\n✅ 今日無新公告，或所有 Pending 皆已清除，任務完成。")
            
        await browser.close()

if __name__ == "__main__": 
    asyncio.run(main())