"""The general page verifier (V0.6 section 11): binding by structure, never by nearness or guess."""

import importlib.util
import os
import unittest

from reflexmesh.verification.page_verifier import PROBE, judge

HAVE_BROWSER = bool(importlib.util.find_spec("playwright") and os.environ.get("REFLEXMESH_CHROME"))

ORDER = ("<nav><a href='/'>Dashboard</a></nav><h1>Заказ №125</h1><p>Customer: Petrov.</p>"
         "<h2>Notes</h2><ul>{notes}</ul><form><label for='n'>Note</label><textarea id='n'></textarea></form>")


class Judge(unittest.TestCase):
    """What a probe result means for each predicate; no browser."""

    def test_unbound_is_unknown_whatever_the_predicate(self):
        for predicate in ("page_match", "scoped_extract", "revisit_match"):
            self.assertEqual(judge(predicate, {"state": "unknown", "reason": "several_anchors"}, "x")["status"],
                             "unknown")
        self.assertEqual(judge("page_match", None, "x")["reason"], "probe_failed")

    def test_match_is_exact_after_spacing(self):
        probe = {"state": "bound", "values": ["  Перезвонить   завтра "]}
        self.assertEqual(judge("page_match", probe, "Перезвонить завтра")["status"], "pass")
        self.assertEqual(judge("page_match", probe, "перезвонить завтра")["status"], "fail")
        self.assertEqual(judge("page_match", {"state": "bound", "values": ["Перезвонить завтра!"]},
                               "Перезвонить завтра")["status"], "fail")

    def test_values_never_reach_the_facts(self):
        verdict = judge("page_match", {"state": "bound", "values": ["secret-ish text"], "anchors": 1}, "nope")
        self.assertNotIn("secret-ish", repr(verdict["facts"]))

    def test_extract_needs_exactly_one_nonempty_value(self):
        self.assertEqual(judge("scoped_extract", {"state": "bound", "values": ["12.50"]}, None)["value"], "12.50")
        self.assertEqual(judge("scoped_extract", {"state": "bound", "values": ["a", "b"]}, None)["status"], "unknown")
        self.assertEqual(judge("scoped_extract", {"state": "bound", "values": [" "]}, None)["status"], "fail")


@unittest.skipUnless(HAVE_BROWSER, "needs playwright and REFLEXMESH_CHROME")
class Probe(unittest.TestCase):
    """The probe in a real Chromium against small pages, one structural case each."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(executable_path=os.environ["REFLEXMESH_CHROME"], args=["--no-sandbox"])
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def probe(self, body: str, anchor: str = "Заказ №125", field: str = "Notes") -> dict:
        self.page.set_content(f"<!doctype html><html><body>{body}</body></html>")
        return self.page.evaluate(f"([a, f]) => ({PROBE})(a, f)", [anchor, field])

    def test_order_page_binds_notes_to_the_order_it_names(self):
        found = self.probe(ORDER.format(notes="<li>Перезвонить завтра</li><li>Other</li>"))
        self.assertEqual((found["state"], found["binding"]), ("bound", "named_container"))
        self.assertEqual(found["values"], ["Перезвонить завтра", "Other"])
        self.assertEqual(judge("page_match", found, "Перезвонить завтра")["facts"]["matches"], 1)

    def test_an_empty_notes_list_is_a_fail_not_an_unknown(self):
        found = self.probe(ORDER.format(notes="<li>No notes yet.</li>"))
        self.assertEqual(judge("page_match", found, "Перезвонить завтра")["status"], "fail")

    def test_a_results_list_has_the_anchor_but_no_field(self):
        found = self.probe("<h1>Orders</h1><ul><li><a href='/orders/125'>Заказ №125</a></li></ul>")
        self.assertEqual((found["state"], found["reason"]), ("unknown", "no_field"))

    def test_the_anchor_inside_a_longer_text_is_not_the_anchor(self):
        found = self.probe("<h1>Заказ №1250</h1><h2>Notes</h2><ul><li>x</li></ul>")
        self.assertEqual(found["reason"], "no_anchor")

    def test_two_anchors_are_ambiguous(self):
        found = self.probe("<p>Заказ №125</p>" + ORDER.format(notes="<li>x</li>"))
        self.assertEqual(found["reason"], "several_anchors")

    def test_each_card_binds_its_own_notes(self):
        card = "<section><h3>{title}</h3><h4>Notes</h4><ul><li>{note}</li></ul></section>"
        body = card.format(title="Заказ №124", note="wrong") + card.format(title="Заказ №125", note="right")
        found = self.probe(body)
        self.assertEqual(found["values"], ["right"])

    def test_nearness_without_a_named_container_is_not_a_binding(self):
        # The anchor sits in a sidebar; the page's own first heading is something else.
        body = ("<h1>Dashboard</h1><aside><p>Заказ №125</p></aside>"
                "<main><h2>Notes</h2><ul><li>Перезвонить завтра</li></ul></main>")
        found = self.probe(body)
        self.assertEqual((found["state"], found["reason"]), ("unknown", "no_structural_link"))

    def test_two_fields_in_one_container_are_ambiguous(self):
        body = "<h1>Заказ №125</h1><h2>Notes</h2><ul><li>a</li></ul><h2>Notes</h2><ul><li>b</li></ul>"
        self.assertEqual(self.probe(body)["reason"], "several_fields")

    def test_a_table_row_binds_by_column(self):
        body = ("<table><thead><tr><th>Order</th><th>Status</th></tr></thead><tbody>"
                "<tr><td>Заказ №124</td><td>Shipped</td></tr><tr><td>Заказ №125</td><td>Processing</td></tr>"
                "</tbody></table>")
        found = self.probe(body, field="Status")
        self.assertEqual((found["binding"], found["values"]), ("table_row", ["Processing"]))
        self.assertEqual(judge("scoped_extract", found, None)["value"], "Processing")

    def test_merged_cells_are_not_guessed(self):
        body = ("<table><tr><th>Order</th><th>Status</th></tr><tr><td colspan='2'>x</td></tr>"
                "<tr><td>Заказ №125</td><td>Processing</td></tr></table>")
        self.assertEqual(self.probe(body, field="Status")["reason"], "merged_cells")

    def test_a_label_reads_its_control_and_a_term_its_definition(self):
        body = "<h1>Заказ №125</h1><label for='s'>Status:</label><input id='s' value='Paid'>"
        self.assertEqual(self.probe(body, field="Status")["values"], ["Paid"])
        body = "<h1>Заказ №125</h1><dl><dt>Status</dt><dd>Paid</dd><dt>Customer</dt><dd>Petrov</dd></dl>"
        self.assertEqual(self.probe(body, field="Status")["values"], ["Paid"])

    def test_hidden_content_is_not_evidence(self):
        body = ORDER.format(notes="<li style='display:none'>Перезвонить завтра</li><li>No notes yet.</li>")
        self.assertEqual(self.probe(body)["values"], ["No notes yet."])

    def test_a_frame_is_reported_and_not_searched(self):
        body = "<h1>Dashboard</h1><iframe srcdoc=\"<h1>Заказ №125</h1><h2>Notes</h2><ul><li>x</li></ul>\"></iframe>"
        found = self.probe(body)
        self.assertEqual((found["reason"], found["frames"]), ("no_anchor", 1))


if __name__ == "__main__":
    unittest.main()
