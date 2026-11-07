import json, re, sys
from playwright.sync_api import sync_playwright
out = open(sys.argv[1], "w", encoding="utf-8")
def text(pg): return re.sub(r"\s+", " ", pg.evaluate("document.body.innerText"))
def run(pg, c):
    r = {}
    pg.goto("https://pypi.org/project/flask/", wait_until="domcontentloaded"); pg.click("text=Release history"); pg.wait_for_timeout(1500)
    s = text(pg); m = re.search(r"1\.0\s+\S*\s*(\w+ \d+, \d{4})", s); r["pypi-history"] = (pg.url, s[s.find("1.0.4")-5:s.find("1.0.4")+40] if "1.0.4" in s else None, m.group(0) if m else None)
    pg.goto("https://arxiv.org/search/advanced", wait_until="domcontentloaded"); pg.fill("input[name='terms-0-term']", "BERT: Pre-training of Deep Bidirectional Transformers"); pg.select_option("select[name='terms-0-field']", "title"); pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state()
    s = text(pg); i = s.find("1810.04805"); r["arxiv-bert"] = (i >= 0, s[max(0,i-10):i+110] if i >= 0 else None, s.count("arXiv:"))
    pg.goto("https://arxiv.org/search/", wait_until="domcontentloaded"); pg.fill("input[name=query]:visible >> nth=-1", "Attention Is All You Need"); pg.select_option("select[name=searchtype]", "title"); pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state()
    s = text(pg); i = s.find("1706.03762"); r["arxiv-attention"] = (i >= 0, s.count("arXiv:"), s[i:i+90] if i>=0 else None)
    pg.goto("https://www.scrapethissite.com/pages/frames/", wait_until="domcontentloaded"); pg.wait_for_timeout(1500)
    f = pg.frame_locator("#iframe"); f.locator("a:has-text('Learn more')").first.click(); pg.wait_for_timeout(1500); r["scrape-turtle-detail"] = re.sub(r"\s+"," ",f.locator("body").inner_text())[:240]
    pg.goto("https://www.gutenberg.org/ebooks/", wait_until="domcontentloaded"); pg.fill("input[name=author]", "Melville"); pg.fill("input[name=title]", "Bartleby"); pg.click("input[name=submit_search]"); pg.wait_for_load_state()
    pg.wait_for_timeout(1500); s = text(pg); i = s.find("Bartleby"); r["gutenberg-adv"] = (pg.url[:90], s[max(0,i-40):i+120] if i>=0 else s[:200], [x.get_attribute("href") for x in pg.locator("a[href*='/ebooks/']").all() if "Bartleby" in (x.inner_text() or "")][:3])
    pg.goto("https://www.gutenberg.org/ebooks/1342", wait_until="domcontentloaded"); s = text(pg); i = s.find("EBook-No"); r["gutenberg-1342"] = (s[i:i+20], s[s.find("Author"):s.find("Author")+30], s[s.find("Release Date"):s.find("Release Date")+30])
    pg.goto("https://quotes.toscrape.com/scroll", wait_until="domcontentloaded"); pg.wait_for_timeout(1000)
    for _ in range(6):
        pg.mouse.wheel(0, 5000); pg.wait_for_timeout(1000)
    r["quotes-last"] = pg.evaluate("[...document.querySelectorAll('.quote')].slice(-3).map(q=>q.querySelector('.author').innerText+' | '+q.querySelector('.text').innerText.slice(0,50))")
    r["quotes-first-page-authors"] = pg.evaluate("[...document.querySelectorAll('.quote')].slice(0,10).map(q=>q.querySelector('.author').innerText)")
    return r
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); c = b.new_context(locale="en-US"); pg = c.new_page(); pg.set_default_timeout(45000)
    try:
        out.write(json.dumps(run(pg, c), ensure_ascii=False, indent=1))
    except Exception as e:
        out.write("ERR " + str(e)[:300])
    out.flush(); b.close()
