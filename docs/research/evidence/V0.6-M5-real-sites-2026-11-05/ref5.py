import json, re, sys
from playwright.sync_api import sync_playwright
out = open(sys.argv[1], "w", encoding="utf-8")
t = lambda x: re.sub(r"\s+", " ", x)
R = {}
def w3sel(pg):
    pg.goto("https://www.w3schools.com/tags/tryit.asp?filename=tryhtml_select", wait_until="domcontentloaded"); pg.wait_for_timeout(3000)
    f = pg.frame_locator("#iframeResult"); before = t(f.locator("body").inner_text())[:80]
    f.locator("select").select_option("audi"); f.locator("input[type=submit]").click(); pg.wait_for_timeout(2500)
    return before, t(f.locator("body").inner_text())[:120]
def widget(pg):
    pg.goto("https://testpages.eviltester.com/pages/web-components/shadow-widget/", wait_until="domcontentloaded"); pg.wait_for_timeout(1500)
    info = pg.evaluate("""() => { const h=[...document.querySelectorAll('*')].filter(e=>e.shadowRoot); return h.map(e=>[e.tagName, e.shadowRoot.mode, e.shadowRoot.innerHTML.replace(/\\s+/g,' ').slice(0,420)]) }""")
    return pg.url, info[:3]
for name, f in [("w3sel", w3sel), ("widget", widget)]:
    pass
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for name, f in [("w3sel", w3sel), ("widget", widget)]:
        c = b.new_context(locale="en-US"); pg = c.new_page(); pg.set_default_timeout(40000)
        try:
            R[name] = f(pg)
        except Exception as e:
            R[name] = "ERR " + str(e)[:160]
        c.close()
    b.close()
out.write(json.dumps(R, ensure_ascii=False, indent=1)); out.close()
