import time
import os
import json
import re
import requests
from playwright.sync_api import sync_playwright

# ==================== 配置项 ====================
LOGIN_URL = "https://dash.hidencloud.com/login"
SERVICE_URL = "https://dash.hidencloud.com/service/232643/manage"  # 你的服务管理页 URL
EMAIL = "your_email@example.com"       # 账号（如果 Cookie 失效会使用）
PASSWORD = "your_password"             # 密码
USE_COOKIE = True                       # 是否优先尝试 Cookie 登录
COOKIE_FILE = "cookies.json"

# Telegram 通知配置（可选）
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# ================================================

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}] {msg}")

def send_tg_notification(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            log("📲 Telegram 通知发送成功")
        else:
            log(f"⚠️ Telegram 通知发送失败: {response.text}")
    except Exception as e:
        log(f"❌ 发送 Telegram 通知异常: {e}")

def handle_cloudflare(page):
    """检测并等待 Cloudflare 盾通过"""
    try:
        if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0 or "Just a moment" in page.title():
            log("🛡️ 检测到 Cloudflare 验证，等待通过...")
            for _ in range(30):
                if page.locator('iframe[src*="challenges.cloudflare.com"]').count() == 0 and "Just a moment" not in page.title():
                    log("✅ Cloudflare 验证已通过")
                    time.sleep(3)
                    return True
                time.sleep(2)
            log("⚠️ Cloudflare 验证等待超时")
            return False
    except Exception:
        pass
    return True

def get_due_date(page):
    """获取当前服务的到期时间"""
    try:
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
        
        content = page.content()
        # 匹配常见日期格式 (例如: 01 Oct 2026)
        match = re.search(r'(\d{2}\s+[A-Za-z]{3}\s+\d{4})', content)
        if match:
            due_date = match.group(1)
            log(f"📅 获取到 Due Date: {due_date}")
            return due_date
    except Exception as e:
        log(f"⚠️ 获取到期时间失败: {e}")
    return "未知"

def renew_service(page):
    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)

        log("🖱️ 寻找并点击 'Renew' 按钮...")
        renew_btn = page.locator('button:has-text("Renew"), a:has-text("Renew"), [role="button"]:has-text("Renew")').first

        modal_opened = False
        for i in range(3):
            try:
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                
                renew_btn.click(force=True)
                time.sleep(2)
                
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    page.screenshot(path="renew_not_allowed.png")
                    return "NOT_TIME"

                # 检查弹窗中的 Create Invoice 按钮是否存在
                create_btn = page.locator('button:has-text("Create Invoice"), a:has-text("Create Invoice")').first
                if create_btn.is_visible():
                    modal_opened = True
                    log("✅ 续费弹窗已成功弹出！")
                    break
                else:
                    log("⚠️ 弹窗未出现，尝试 JS 强制点击 Renew...")
                    renew_btn.evaluate("el => el.click()")
                    time.sleep(2)
                    if create_btn.is_visible():
                        modal_opened = True
                        log("✅ JS 强制点击成功，弹窗已弹出！")
                        break
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")
                time.sleep(2)

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            page.screenshot(path="renew_modal_failed.png")
            return False

        handle_cloudflare(page)
        
        # 定位弹窗内的 Create Invoice 按钮
        create_btn = page.locator('button:has-text("Create Invoice"), a:has-text("Create Invoice")').first
        log("🖱️ 点击 'Create Invoice' 并等待发票生成跳转...")
        create_btn.scroll_into_view_if_needed()
        create_btn.click(force=True)

        # 严格等待 URL 跳转到发票/支付详情页
        start_wait = time.time()
        invoice_page_reached = False
        
        while time.time() - start_wait < 30:
            current_url = page.url
            log(f"⏳ 正在等待跳转发票页... 当前 URL: {current_url}")
            
            if "/service/" not in current_url and any(k in current_url.lower() for k in ["invoice", "pay", "billing"]):
                invoice_page_reached = True
                log(f"🎉 成功进入发票页面: {current_url}")
                break
                
            if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                handle_cloudflare(page)
                
            time.sleep(2)

        # 降级兜底方案：如果没自动跳转，主动前往 Invoices 列表寻找未支付发票
        if not invoice_page_reached:
            log("⚠️ 页面未自动跳转发票页，尝试前往 Invoices 列表确认...")
            page.screenshot(path="before_invoice_check.png")
            
            invoices_nav = page.locator('a:has-text("Invoices"), button:has-text("Invoices")').first
            if invoices_nav.is_visible():
                invoices_nav.click()
                time.sleep(3)
                handle_cloudflare(page)
                
                first_pay_btn = page.locator('a:has-text("Pay"), button:has-text("Pay"), a:has-text("Unpaid")').first
                if first_pay_btn.is_visible():
                    first_pay_btn.click()
                    time.sleep(3)
                    invoice_page_reached = True

        if not invoice_page_reached:
            log("❌ 未能进入发票页面，发票生成失败。")
            page.screenshot(path="renew_create_invoice_failed.png")
            return False

        handle_cloudflare(page)

        log("🔎 查找并点击支付页的 'Pay' 按钮...")
        pay_btn = page.locator('button:has-text("Pay"), a:has-text("Pay"), input[value="Pay"]').first
        pay_btn.wait_for(state="visible", timeout=15000)
        
        pay_btn.scroll_into_view_if_needed()
        time.sleep(1)
        try:
            pay_btn.click(force=True)
        except Exception:
            log("⚠️ 执行 JS 强制点击 'Pay'...")
            pay_btn.evaluate("el => el.click()")
            
        log("✅ 'Pay' 按钮已点击，等待 5 秒结算完成...")
        time.sleep(5)

        # 回到服务页确认
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        page.screenshot(path="renew_error.png")
        return False

def main():
    log("🚀 启动浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # 登录逻辑
            global USE_COOKIE
            if USE_COOKIE and os.path.exists(COOKIE_FILE):
                log("📇 尝试 Cookie 登录...")
                with open(COOKIE_FILE, "r") as f:
                    cookies = json.load(f)
                context.add_cookies(cookies)
                page.goto("https://dash.hidencloud.com/dashboard", wait_until="domcontentloaded", timeout=60000)
                handle_cloudflare(page)
                
                if "login" in page.url.lower():
                    log("⚠️ Cookie 已失效，转为账号密码登录...")
                    USE_COOKIE = False

            if not USE_COOKIE or not os.path.exists(COOKIE_FILE):
                log("🔑 使用账号密码登录...")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                handle_cloudflare(page)
                page.fill('input[name="email"]', EMAIL)
                page.fill('input[name="password"]', PASSWORD)
                page.click('button[type="submit"]')
                time.sleep(5)
                handle_cloudflare(page)
                
                # 保存 Cookie
                cookies = context.cookies()
                with open(COOKIE_FILE, "w") as f:
                    json.dump(cookies, f)

            log(f"✅ 登录成功！当前 URL: {page.url}")

            # 获取续费前到期时间
            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            # 执行续费
            result = renew_service(page)
            
            if result == True:
                time.sleep(5)
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                send_tg_notification(f"✅ HidenCloud 服务续费成功！\n续费前到期: {old_due}\n续费后到期: {new_due}")
            elif result == "NOT_TIME":
                log("ℹ️ 提示：目前还未到可续费时间窗口。")
            else:
                log("❌ 续费流程执行失败，请检查截图。")
                send_tg_notification("❌ HidenCloud 服务续费执行失败，请检查服务器截图。")

        except Exception as e:
            log(f"❌ 运行发生致命错误: {e}")
            page.screenshot(path="fatal_error.png")
            send_tg_notification(f"❌ HidenCloud 续费脚本发生异常: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
