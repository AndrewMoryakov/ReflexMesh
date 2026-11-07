import re, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(); pg.set_default_timeout(40000)
    pg.goto("https://testpages.eviltester.com/pages/web-components/shadow-widget/", wait_until="domcontentloaded"); pg.wait_for_timeout(1500)
    q = """() => { const r=[...document.querySelectorAll('*')].find(e=>e.shadowRoot).shadowRoot; return [r.textContent.replace(/\\s+/g,' ').slice(-120), [...r.querySelectorAll('button')].map(x=>x.innerText)] }"""
    print(pg.evaluate(q))
    r = pg.locator("button:has-text('Increment'), button:has-text('+')").first; r.click(); r.click(); pg.wait_for_timeout(300); print(pg.evaluate(q)[0])
    b.close()
