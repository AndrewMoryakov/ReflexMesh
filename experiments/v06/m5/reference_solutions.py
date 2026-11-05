"""Can the oracle tell? For each real-site task: plain Playwright, not the ReflexMesh adapter, reaches the task's end, and the
runner's own oracle must say yes there and no on the start page. Run it right before freezing the set.

    python experiments/v06/m5/reference_solutions.py OUT.jsonl
"""

import importlib.util
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("m5_runner", ROOT / "experiments/v06/m5/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def search(pg, selector, value):
    pg.fill(selector, value)
    pg.press(selector, "Enter")
    pg.wait_for_load_state("domcontentloaded")


def new_tab(pg, context, link):
    with context.expect_page() as popup:
        pg.click(link)
    popup.value.wait_for_load_state("domcontentloaded")
    return popup.value


def arxiv(pg, identifier):
    pg.click(f"a[href*='{identifier}']")
    pg.wait_for_load_state("domcontentloaded")


def wiki_everest(pg, c): search(pg, "input[name=search]", "Mount Everest")
def wiki_lovelace(pg, c): search(pg, "input[name=search]", "Ada Lovelace")
def wiki_help(pg, c): return new_tab(pg, c, "a[target=_blank]:has-text('Help')")
def arxiv_resnet(pg, c):
    pg.fill("input[name=query]:visible >> nth=-1", "Deep Residual Learning for Image Recognition"); pg.select_option("select[name=searchtype]", "title")
    pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state("domcontentloaded"); arxiv(pg, "1512.03385")
def arxiv_bert(pg, c):
    pg.fill("input[name='terms-0-term']", "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"); pg.select_option("select[name='terms-0-field']", "title")
    pg.click("button:has-text('Search'):visible >> nth=-1"); pg.wait_for_load_state("domcontentloaded"); arxiv(pg, "1810.04805")
def arxiv_bibtex(pg, c): pg.click("text=Export BibTeX Citation"); pg.wait_for_timeout(4000)
def pypi_history(pg, c): pg.click("text=Release history"); pg.wait_for_timeout(1500)
def pypi_psf(pg, c): return new_tab(pg, c, "a[target=_blank]:has-text('Python Software Foundation')")
def pypi_guide(pg, c): return new_tab(pg, c, "a[target=_blank]:has-text('Packaging user guide')")
def w3_tab(pg, c): return new_tab(pg, c, "a[target=_blank]:has-text('Try it Yourself') >> nth=0")
def w3_form(pg, c):
    f = pg.frame_locator("#iframeResult"); f.locator("input[name=fname]").fill("Ada"); f.locator("input[type=submit]").click(); pg.wait_for_timeout(2500)
def w3_select(pg, c):
    f = pg.frame_locator("#iframeResult"); f.locator("select").select_option("audi"); f.locator("input[type=submit]").click(); pg.wait_for_timeout(2500)
def internet_loading(pg, c): pg.click("button:has-text('Start')"); pg.wait_for_selector("#finish", state="visible", timeout=20000)
def internet_controls(pg, c):
    pg.click("button:has-text('Enable')")
    pg.wait_for_function("document.querySelector('#input-example input') && !document.querySelector('#input-example input').disabled", timeout=20000); pg.wait_for_timeout(500)
def internet_window(pg, c): return new_tab(pg, c, "text=Click Here")
def scrape_hockey(pg, c): pg.fill("#q", "Rangers"); pg.click("input[type=submit]"); pg.wait_for_load_state("domcontentloaded")
def scrape_oscars(pg, c): pg.click("a:has-text('2015')"); pg.wait_for_timeout(3000)
def scrape_turtles(pg, c): pg.frame_locator("#iframe").locator("a:has-text('Learn more')").first.click(); pg.wait_for_timeout(2000)
def quotes_next(pg, c): pg.click("li.next a"); pg.wait_for_timeout(1500)
def quotes_scroll(pg, c):
    for _ in range(8):
        pg.mouse.wheel(0, 5000); pg.wait_for_timeout(1000)
def quotes_filter(pg, c):
    pg.select_option("#author", "Albert Einstein"); pg.wait_for_timeout(1500); pg.select_option("#tag", "inspirational"); pg.click("input[type=submit]"); pg.wait_for_load_state("domcontentloaded"); pg.wait_for_timeout(800)
def selenium_form(pg, c):
    pg.fill("#my-text-id", "ReflexMesh test"); pg.select_option("select[name=my-select]", label="Two"); pg.click("button[type=submit]"); pg.wait_for_load_state("domcontentloaded"); pg.wait_for_timeout(500)
def selenium_window(pg, c): return new_tab(pg, c, "text=Create a new anonymous window")
def selenium_shadow(pg, c): pg.locator("custom-checkbox-element input").check()
def testpages_form(pg, c):
    pg.fill("input[name=username]", "reflexmesh"); pg.fill("textarea[name=comments]", "Hello from ReflexMesh"); pg.click("input[type=submit]"); pg.wait_for_load_state("domcontentloaded"); pg.wait_for_timeout(1000)
def testpages_frame(pg, c): pg.frame_locator("iframe").nth(1).locator("#incBtn").click(); pg.wait_for_timeout(500)
def testpages_shadow(pg, c):
    b = pg.locator("button:has-text('Increase')").first; b.click(); b.click(); pg.wait_for_timeout(300)
def gutenberg_book(title):
    def go(pg, c):
        search(pg, "input[name=query] >> nth=-1", title); pg.click("li.booklink a"); pg.wait_for_load_state("domcontentloaded")
    return go
def gutenberg_advanced(pg, c):
    pg.fill("input[name=author]", "Melville"); pg.fill("input[name=title]", "Bartleby"); pg.click("input[name=submit_search]"); pg.wait_for_load_state("domcontentloaded"); pg.wait_for_timeout(1000)


PATHS = {"wiki-everest": wiki_everest, "wiki-lovelace": wiki_lovelace, "wiki-help-tab": wiki_help, "arxiv-resnet": arxiv_resnet, "arxiv-bert": arxiv_bert,
         "arxiv-bibtex": arxiv_bibtex, "pypi-history": pypi_history, "pypi-psf-tab": pypi_psf, "pypi-guide-tab": pypi_guide, "w3-tryit-tab": w3_tab,
         "w3-form-frame": w3_form, "w3-select-frame": w3_select, "internet-loading": internet_loading, "internet-controls": internet_controls,
         "internet-window": internet_window, "scrape-hockey": scrape_hockey, "scrape-oscars": scrape_oscars, "scrape-turtles": scrape_turtles,
         "quotes-next": quotes_next, "quotes-scroll": quotes_scroll, "quotes-filter": quotes_filter, "selenium-form": selenium_form,
         "selenium-window": selenium_window, "selenium-shadow": selenium_shadow, "testpages-form": testpages_form, "testpages-frame": testpages_frame,
         "testpages-shadow": testpages_shadow, "gutenberg-moby": gutenberg_book("Moby Dick"), "gutenberg-pride": gutenberg_book("Pride and Prejudice"),
         "gutenberg-advanced": gutenberg_advanced}


def main() -> int:
    manifest = json.loads((ROOT / "experiments/v06/m5/manifest.real-sites.json").read_text("utf-8"))
    bad = 0
    with sync_playwright() as p, open(sys.argv[1], "w", encoding="utf-8") as out:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for task in manifest["tasks"]:
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.set_default_timeout(45000)
            row = {"task": task["id"]}
            try:
                page.goto(task["start_url"], wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                row["at_start"] = runner.judge_final(task["oracle"], page.evaluate(runner.FINAL_STATE_JS))
                end = PATHS[task["id"]](page, context) or page
                row["at_end"] = runner.judge_final(task["oracle"], end.evaluate(runner.FINAL_STATE_JS))
                row["url"] = end.url
            except Exception as exc:
                row["error"] = str(exc)[:200]
            ok = row.get("at_end", [None])[0] is True and row.get("at_start", [True])[0] is False
            bad += not ok
            out.write(json.dumps({**row, "ok": ok}, ensure_ascii=False) + "\n")
            out.flush()
            print("ok " if ok else "BAD", json.dumps(row, ensure_ascii=False)[:170], flush=True)
            context.close()
        browser.close()
    print("bad:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
