"""Read-only look at candidate sites with plain Playwright, not the ReflexMesh adapter: what a task author needs."""
import json, sys
from playwright.sync_api import sync_playwright
URLS = """
https://en.wikipedia.org/wiki/Main_Page
https://en.wikipedia.org/wiki/Mount_Everest
https://www.wikidata.org/wiki/Q513
https://arxiv.org/abs/1706.03762
https://arxiv.org/search/
https://pypi.org/project/requests/
https://docs.python.org/3/library/json.html
https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe
https://developer.mozilla.org/en-US/docs/Web/API/Web_components/Using_shadow_DOM
https://www.w3schools.com/html/tryit.asp?filename=tryhtml_iframe
https://www.w3schools.com/html/html_iframe.asp
https://the-internet.herokuapp.com/
https://the-internet.herokuapp.com/dynamic_loading/1
https://the-internet.herokuapp.com/windows
https://the-internet.herokuapp.com/shadowdom
https://the-internet.herokuapp.com/iframe
https://the-internet.herokuapp.com/nested_frames
https://the-internet.herokuapp.com/login
https://the-internet.herokuapp.com/dropdown
https://the-internet.herokuapp.com/add_remove_elements/
https://books.toscrape.com/
https://quotes.toscrape.com/js/
https://quotes.toscrape.com/login
https://quotes.toscrape.com/search.aspx
https://quotes.toscrape.com/scroll
https://www.selenium.dev/selenium/web/web-form.html
https://www.selenium.dev/selenium/web/shadowRootPage.html
https://www.selenium.dev/selenium/web/iframes.html
https://www.selenium.dev/selenium/web/xhtmlTest.html
https://httpbin.org/forms/post
https://demoqa.com/text-box
https://demoqa.com/browser-windows
https://demoqa.com/frames
https://demoqa.com/dynamic-properties
https://uitestingplayground.com/
https://uitestingplayground.com/ajax
https://uitestingplayground.com/shadowdom
https://uitestingplayground.com/textinput
https://uitestingplayground.com/dynamicid
https://www.saucedemo.com/
https://practice.expandtesting.com/
https://practice.expandtesting.com/windows
https://practice.expandtesting.com/shadowdom
https://practice.expandtesting.com/iframe
https://testpages.eviltester.com/styled/index.html
https://testpages.eviltester.com/styled/basic-html-form-test.html
https://testpages.eviltester.com/styled/iframes-test.html
https://testpages.eviltester.com/styled/windows-test.html
https://testpages.eviltester.com/styled/dynamic-buttons-simple.html
https://www.openstreetmap.org/
https://news.ycombinator.com/
https://www.gutenberg.org/ebooks/1342
https://www.gutenberg.org/ebooks/search/
https://www.scrapethissite.com/pages/
https://www.scrapethissite.com/pages/forms/
https://www.scrapethissite.com/pages/ajax-javascript/
https://www.scrapethissite.com/pages/frames/
https://webscraper.io/test-sites/e-commerce/static
""".split()
SUMMARY = """() => {
  let shadow = 0; const walk = (root) => { for (const e of root.querySelectorAll('*')) { if (e.shadowRoot) { shadow++; walk(e.shadowRoot); } } }; walk(document);
  const label = (e) => { const l = e.labels && e.labels[0] ? e.labels[0].innerText : ''; return (l || e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.name || e.id || e.type || '').trim().slice(0,40); };
  return {title: document.title.slice(0,70), text: document.body ? document.body.innerText.length : 0, iframes: document.querySelectorAll('iframe,frame').length, shadow,
    inputs: [...document.querySelectorAll('input:not([type=hidden]),textarea,select')].slice(0,10).map(e => e.tagName.toLowerCase()+':'+label(e)),
    forms: [...document.querySelectorAll('form')].slice(0,3).map(f => (f.method||'get').toLowerCase()+' '+(f.getAttribute('action')||'').slice(0,50)),
    blank: document.querySelectorAll('a[target=_blank]').length, scripts: document.scripts.length,
    captcha: /captcha|verify you are human|just a moment|cloudflare/i.test(document.body ? document.body.innerText.slice(0,2000) : '')};
}"""
out = open(sys.argv[1], "w", encoding="utf-8")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    c = b.new_context(ignore_https_errors=False, locale="en-US")
    for url in URLS:
        pg = c.new_page()
        row = {"url": url}
        try:
            r = pg.goto(url, timeout=30000, wait_until="load")
            pg.wait_for_timeout(1200)
            row.update(status=r.status if r else None, final=pg.url, **pg.evaluate(SUMMARY))
        except Exception as e:
            row.update(error=type(e).__name__, msg=str(e)[:100])
        out.write(json.dumps(row, ensure_ascii=True) + "\n"); out.flush()
        pg.close()
    b.close()
