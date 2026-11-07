"""Reference solutions with plain Playwright: is each task doable, and what is the right answer. Never fed to any mode."""
import json, re, sys
from playwright.sync_api import sync_playwright
out = open(sys.argv[1], "w", encoding="utf-8")
def text(pg): return re.sub(r"\s+", " ", pg.evaluate("document.body.innerText"))
def has(s, word): return word in s
def find(s, pat, n=120):
    m = re.search(pat, s); return s[max(0,m.start()-20):m.end()+n] if m else None
TASKS = {}
def task(name):
    def deco(f): TASKS[name] = f; return f
    return deco
@task("wiki-everest")
def _(pg, c):
    pg.goto("https://en.wikipedia.org/wiki/Main_Page"); pg.fill("input[name=search]", "Mount Everest"); pg.press("input[name=search]", "Enter"); pg.wait_for_load_state()
    return pg.url, find(text(pg), r"Elevation", 40)
@task("wiki-lovelace")
def _(pg, c):
    pg.goto("https://en.wikipedia.org/wiki/Main_Page"); pg.fill("input[name=search]", "Ada Lovelace"); pg.press("input[name=search]", "Enter"); pg.wait_for_load_state()
    return pg.url, find(text(pg), r"Born", 50)
@task("wiki-help-tab")
def _(pg, c):
    pg.goto("https://en.wikipedia.org/wiki/Special:Search")
    with c.expect_page() as p: pg.click("a[target=_blank]:has-text('Help')")
    n = p.value; n.wait_for_load_state(); return n.url, n.title(), text(n)[:0], find(text(n), r"Help:Searching", 60)
@task("arxiv-search")
def _(pg, c):
    pg.goto("https://arxiv.org/search/"); pg.fill("input[name=query]:visible >> nth=-1", "Attention Is All You Need"); pg.select_option("select[name=searchtype]", "title"); pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state()
    s = text(pg); pg.click("p.list-title a"); pg.wait_for_load_state(); return pg.url, find(text(pg), r"Attention Is All You Need", 40), s[:0]
@task("arxiv-advanced")
def _(pg, c):
    pg.goto("https://arxiv.org/search/advanced"); pg.fill("input[name='terms-0-term']", "BERT: Pre-training of Deep Bidirectional Transformers"); pg.select_option("select[name='terms-0-field']", "title"); pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state()
    return pg.url[:30], find(text(pg), r"arXiv:\d", 90)
@task("arxiv-bibtex")
def _(pg, c):
    pg.goto("https://arxiv.org/abs/1706.03762"); pg.click("text=Export BibTeX Citation"); pg.wait_for_timeout(3000)
    return pg.url, pg.evaluate("[...document.querySelectorAll('textarea')].map(t=>t.value||t.textContent).join('|').slice(0,160)")
@task("pypi-search")
def _(pg, c):
    pg.goto("https://pypi.org/"); pg.fill("input[name=q]", "flask"); pg.press("input[name=q]", "Enter"); pg.wait_for_timeout(5000)
    return pg.url, pg.title(), text(pg)[:220]
@task("pypi-psf-tab")
def _(pg, c):
    pg.goto("https://pypi.org/project/flask/")
    with c.expect_page() as p: pg.click("a[target=_blank]:has-text('Python Software Foundation')")
    n = p.value; n.wait_for_load_state(); return n.url, n.title()
@task("pypi-guide-tab")
def _(pg, c):
    pg.goto("https://pypi.org/project/flask/")
    with c.expect_page() as p: pg.click("a[target=_blank]:has-text('Packaging user guide')")
    n = p.value; n.wait_for_load_state(); return n.url, n.title()
@task("w3-tryit-tab")
def _(pg, c):
    pg.goto("https://www.w3schools.com/html/html_iframe.asp")
    with c.expect_page() as p: pg.click("a[target=_blank]:has-text('Try it Yourself') >> nth=0")
    n = p.value; n.wait_for_load_state(); return n.url, n.title()
@task("w3-iframe-result")
def _(pg, c):
    pg.goto("https://www.w3schools.com/html/tryit.asp?filename=tryhtml_iframe"); pg.wait_for_timeout(2000)
    f = pg.frame_locator("#iframeResult"); return pg.url, f.locator("h2").first.inner_text(), f.locator("p").first.inner_text()
@task("w3-basic-result")
def _(pg, c):
    pg.goto("https://www.w3schools.com/html/tryit.asp?filename=tryhtml_basic"); pg.wait_for_timeout(2000)
    f = pg.frame_locator("#iframeResult"); return pg.url, re.sub(r"\s+"," ",f.locator("body").inner_text())[:120]
@task("internet-dynamic")
def _(pg, c):
    pg.goto("https://the-internet.herokuapp.com/dynamic_loading/1"); pg.click("button:has-text('Start')"); pg.wait_for_selector("#finish", state="visible", timeout=15000); return pg.url, pg.inner_text("#finish")
@task("internet-window")
def _(pg, c):
    pg.goto("https://the-internet.herokuapp.com/windows")
    with c.expect_page() as p: pg.click("text=Click Here")
    n = p.value; n.wait_for_load_state(); return n.url, n.title(), n.inner_text("h3")
@task("internet-shadow")
def _(pg, c):
    pg.goto("https://the-internet.herokuapp.com/shadowdom")
    return pg.url, pg.evaluate("[...document.querySelectorAll('my-paragraph')].map(e=>[e.innerText, e.shadowRoot && e.shadowRoot.textContent.replace(/\\s+/g,' ').slice(0,80)])")
@task("scrape-hockey")
def _(pg, c):
    pg.goto("https://www.scrapethissite.com/pages/forms/"); pg.fill("#q", "Rangers"); pg.click("input[type=submit]"); pg.wait_for_load_state()
    return pg.url, find(text(pg), r"New York Rangers", 40)
@task("scrape-oscars")
def _(pg, c):
    pg.goto("https://www.scrapethissite.com/pages/ajax-javascript/"); pg.click("a:has-text('2015')"); pg.wait_for_timeout(3000)
    return pg.url, find(text(pg), r"Title Nominations", 120)
@task("scrape-turtles")
def _(pg, c):
    pg.goto("https://www.scrapethissite.com/pages/frames/"); pg.wait_for_timeout(1500)
    f = pg.frame_locator("#iframe"); return pg.url, re.sub(r"\s+"," ",f.locator("body").inner_text())[:260]
@task("quotes-js")
def _(pg, c):
    pg.goto("https://quotes.toscrape.com/js/"); pg.wait_for_timeout(1500); return pg.url, pg.inner_text(".quote .author"), pg.locator(".quote").count()
@task("quotes-scroll")
def _(pg, c):
    pg.goto("https://quotes.toscrape.com/scroll"); pg.wait_for_timeout(1500)
    for _ in range(6):
        pg.mouse.wheel(0, 5000); pg.wait_for_timeout(1200)
    return pg.url, pg.locator(".quote").count(), pg.evaluate("[...document.querySelectorAll('.quote')].map(q=>q.querySelector('.author').innerText).slice(0,60).join(', ')")
@task("quotes-filter")
def _(pg, c):
    pg.goto("https://quotes.toscrape.com/search.aspx"); pg.select_option("#author", "Albert Einstein"); pg.wait_for_timeout(1500); pg.select_option("#tag", "inspirational"); pg.click("input[type=submit]"); pg.wait_for_load_state(); pg.wait_for_timeout(800)
    return pg.url, re.sub(r"\s+"," ",pg.inner_text(".quote"))[:200], pg.locator(".quote").count()
@task("selenium-form")
def _(pg, c):
    pg.goto("https://www.selenium.dev/selenium/web/web-form.html"); pg.fill("#my-text-id", "ReflexMesh test"); pg.select_option("select[name=my-select]", label="Two"); pg.click("button[type=submit]"); pg.wait_for_load_state()
    return pg.url, pg.inner_text("h1"), pg.inner_text("#message")
@task("selenium-window")
def _(pg, c):
    pg.goto("https://www.selenium.dev/selenium/web/xhtmlTest.html")
    with c.expect_page() as p: pg.click("text=Create a new anonymous window")
    n = p.value; n.wait_for_load_state(); return n.url, n.title(), text(n)[:120]
@task("selenium-shadow")
def _(pg, c):
    pg.goto("https://www.selenium.dev/selenium/web/shadowRootPage.html"); pg.wait_for_timeout(800)
    return pg.url, pg.evaluate("[...document.querySelectorAll('*')].filter(e=>e.shadowRoot).map(e=>e.tagName+':'+e.shadowRoot.innerHTML.replace(/\\s+/g,' ').slice(0,200))"), pg.content().replace("\n"," ")[:260]
@task("testpages-form")
def _(pg, c):
    pg.goto("https://testpages.eviltester.com/styled/basic-html-form-test.html"); pg.fill("input[name=username]", "reflexmesh"); pg.fill("input[name=password]", "not-a-secret"); pg.fill("textarea[name=comments]", "Hello from ReflexMesh"); pg.click("input[type=submit]"); pg.wait_for_load_state(); pg.wait_for_timeout(1000)
    return pg.url, find(text(pg), r"Processed Form Details", 200), find(text(pg), r"Hello from ReflexMesh", 10)
@task("testpages-buttons")
def _(pg, c):
    pg.goto("https://testpages.eviltester.com/styled/dynamic-buttons-simple.html")
    for b in ("#button00", "#button01", "#button02", "#button03"):
        pg.wait_for_selector(b, state="visible", timeout=8000); pg.click(b)
    pg.wait_for_timeout(800); return pg.url, pg.inner_text("#buttonmessage")
@task("testpages-iframe")
def _(pg, c):
    pg.goto("https://testpages.eviltester.com/styled/iframes-test.html"); pg.wait_for_timeout(1500)
    f = pg.frame_locator("iframe").first; return pg.url, re.sub(r"\s+"," ",f.locator("body").inner_text())[:200]
@task("gutenberg-melville")
def _(pg, c):
    pg.goto("https://www.gutenberg.org/ebooks/search/"); pg.fill("input[name=query] >> nth=-1", "Moby Dick"); pg.press("input[name=query] >> nth=-1", "Enter"); pg.wait_for_load_state()
    return pg.url, re.sub(r"\s+"," ",pg.inner_text("li.booklink"))[:120], pg.locator("li.booklink a").first.get_attribute("href")
@task("gutenberg-pride")
def _(pg, c):
    pg.goto("https://www.gutenberg.org/ebooks/search/"); pg.fill("input[name=query] >> nth=-1", "Pride and Prejudice"); pg.press("input[name=query] >> nth=-1", "Enter"); pg.wait_for_load_state()
    pg.click("li.booklink a"); pg.wait_for_load_state(); return pg.url, find(text(pg), r"EBook-No\.", 12), find(text(pg), r"Author", 30)
@task("gutenberg-advanced")
def _(pg, c):
    pg.goto("https://www.gutenberg.org/ebooks/"); s = text(pg)
    fields = pg.evaluate("[...document.querySelectorAll('form input,form select')].map(e=>e.tagName+':'+e.name+':'+e.type+':'+(e.labels&&e.labels[0]?e.labels[0].innerText:'')).slice(0,14)")
    return pg.url, fields, s[:0]
ONLY = {"pypi-search","w3-basic-result","internet-dynamic","internet-window","internet-shadow","scrape-turtles","testpages-iframe","gutenberg-melville","gutenberg-pride","gutenberg-advanced","arxiv-advanced"}
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for name, f in TASKS.items():
        if name not in ONLY: continue
        c = b.new_context(locale="en-US"); pg = c.new_page(); pg.set_default_timeout(45000)
        _goto = pg.goto
        pg.goto = lambda url, **kw: _goto(url, wait_until="domcontentloaded", **kw)
        try:
            out.write(json.dumps({"task": name, "ok": True, "ans": f(pg, c)}, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            out.write(json.dumps({"task": name, "ok": False, "err": str(e)[:220]}) + "\n")
        out.flush(); c.close()
    b.close()
