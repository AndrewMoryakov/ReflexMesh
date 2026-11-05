import json, re, sys
from playwright.sync_api import sync_playwright
out = open(sys.argv[1], "w", encoding="utf-8")
def text(x): return re.sub(r"\s+", " ", x)
R = {}
def step(name, f, pg):
    try:
        R[name] = f(pg)
    except Exception as e:
        R[name] = "ERR " + str(e)[:160]
def resnet(pg):
    pg.goto("https://arxiv.org/search/", wait_until="domcontentloaded"); pg.fill("input[name=query]:visible >> nth=-1", "Deep Residual Learning for Image Recognition"); pg.select_option("select[name=searchtype]", "title"); pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state()
    s = text(pg.inner_text("body")); i = s.find("1512.03385"); return (i >= 0, s.count("arXiv:"), s[i:i+80])
def w3form(pg):
    pg.goto("https://www.w3schools.com/html/tryit.asp?filename=tryhtml_form_submit", wait_until="domcontentloaded"); pg.wait_for_timeout(2500)
    f = pg.frame_locator("#iframeResult"); before = text(f.locator("body").inner_text())[:120]
    f.locator("input[name=fname]").fill("Ada"); f.locator("input[type=submit]").click(); pg.wait_for_timeout(2500)
    return before, text(f.locator("body").inner_text())[:200]
def w3check(pg):
    pg.goto("https://www.w3schools.com/html/tryit.asp?filename=tryhtml_form_checkbox", wait_until="domcontentloaded"); pg.wait_for_timeout(2500)
    f = pg.frame_locator("#iframeResult"); before = text(f.locator("body").inner_text())[:90]
    f.locator("input[type=checkbox]").first.check(); f.locator("input[type=submit]").click(); pg.wait_for_timeout(2500)
    return before, text(f.locator("body").inner_text())[:200]
def tpframe(pg):
    pg.goto("https://testpages.eviltester.com/styled/iframes-test.html", wait_until="domcontentloaded"); pg.wait_for_timeout(2000)
    f = pg.frame_locator("iframe").nth(1); s = text(f.locator("body").inner_text())[:200]
    html = f.locator("body").inner_html()[:500]; return pg.url, s, re.sub(r"\s+"," ",html)[:360]
def tpindex(pg):
    pg.goto("https://testpages.eviltester.com/styled/index.html", wait_until="domcontentloaded"); pg.wait_for_timeout(800)
    return [a for a in pg.evaluate("[...document.querySelectorAll('a')].map(a=>a.innerText.trim()+' -> '+a.getAttribute('href'))") if re.search(r"(?i)shadow|frame", a)][:14]
def controls(pg):
    pg.goto("https://the-internet.herokuapp.com/dynamic_controls", wait_until="domcontentloaded"); pg.click("button:has-text('Remove')"); pg.wait_for_selector("#message", timeout=15000); a = pg.inner_text("#message")
    pg.click("button:has-text('Enable')"); pg.wait_for_function("document.querySelector('#input-example input') && !document.querySelector('#input-example input').disabled", timeout=15000); pg.wait_for_timeout(500); return a, pg.inner_text("#input-example #message")
def qjs(pg):
    pg.goto("https://quotes.toscrape.com/js/", wait_until="domcontentloaded"); pg.wait_for_timeout(1200); pg.click("li.next a"); pg.wait_for_timeout(1500)
    return pg.url, pg.inner_text(".quote .author"), pg.inner_text(".quote .text")[:60]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for name, f in [("resnet", resnet), ("w3form", w3form), ("w3check", w3check), ("tpframe", tpframe), ("tpindex", tpindex), ("controls", controls), ("qjs", qjs)]:
        c = b.new_context(locale="en-US"); pg = c.new_page(); pg.set_default_timeout(40000); step(name, f, pg); c.close()
    b.close()
out.write(json.dumps(R, ensure_ascii=False, indent=1)); out.close()
