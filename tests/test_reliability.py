import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import bot


class ExtractionTests(unittest.TestCase):
    FIXTURES = Path(__file__).parent / "fixtures"

    def test_rent_formats_and_ancillary_costs(self):
        self.assertEqual(bot.parse_rent("Huur € 1.850,- per maand"), 1850)
        self.assertEqual(bot.parse_rent("EUR 2 000 p/m"), 2000)
        self.assertIsNone(bot.parse_rent("Servicekosten € 150 per maand"))

    def test_area_and_rooms(self):
        self.assertEqual(bot.parse_area("Wonen 72,4 m²"), 72)
        self.assertEqual(bot.parse_area("80 m2"), 80)
        self.assertEqual(bot.parse_rooms("3 kamers"), 3)

    def test_jsonld_relative_url(self):
        soup = bot.BeautifulSoup('<script type="application/ld+json">{"name":"Test","url":"/detail/huur/test/1","address":{"addressLocality":"Utrecht"},"description":"€ 1.500, 65 m²"}</script>', "lxml")
        listing = bot.extract_jsonld_listings("funda", "https://funda.nl/zoeken", soup)[0]
        self.assertEqual(listing.url, "https://funda.nl/detail/huur/test/1")
        self.assertEqual((listing.rent, listing.area), (1500, 65))

    def test_funda_fixture(self):
        soup = bot.BeautifulSoup((self.FIXTURES / "funda_results.html").read_text(encoding="utf-8"), "lxml")
        listing = bot.extract_from_blocks("funda", "https://www.funda.nl", soup)[0]
        self.assertEqual((listing.rent, listing.area, listing.rooms), (1750, 72, 3))


class DedupeAndFilterTests(unittest.TestCase):
    def test_merge_complements_without_erasing(self):
        a = bot.Listing("funda", "Kort", "https://x.nl/w/1?utm_source=a", "Utrecht", 1500, None, 3)
        b = bot.Listing("funda", "Veel langere titel", "https://x.nl/w/1#foto", None, None, 70, None)
        merged = bot.dedupe_listings([a, b])[0]
        self.assertEqual((merged.rent, merged.area, merged.rooms), (1500, 70, 3))
        self.assertEqual(merged.title, "Veel langere titel")

    def test_overlapping_reasons_and_missing_flags(self):
        item = bot.Listing("x", "Andere plaats", "https://x.nl/1")
        filters = {"city": "Utrecht", "max_rent": 2000, "min_area": 60}
        self.assertEqual(set(bot.filter_reasons(item, filters)), {"wrong_city", "missing_rent", "missing_area"})
        filters.update(allow_missing_rent=True, allow_missing_area=True)
        self.assertEqual(bot.filter_reasons(item, filters), ["wrong_city"])


class HttpHealthTests(unittest.TestCase):
    def test_block_signals(self):
        for text in ("Access denied because this request was blocked", "verify you are human before continuing"):
            with self.assertRaises(bot.BlockedPageError):
                bot.validate_html_response("<html>" + text + "</html>", source="x", url="https://x.nl")

    def test_short_html(self):
        with self.assertRaises(bot.InvalidHtmlResponseError):
            bot.validate_html_response("<html>ok</html>", source="x", url="https://x.nl")

    def test_security_library_words_are_not_automatically_blocked(self):
        html = "<html><head><title>Woningen</title><script>const vendor='cloudflare captcha';</script></head><body><h1>Beschikbare huurwoningen</h1><p>Bekijk hieronder ons actuele woningaanbod in Utrecht.</p></body></html>"
        bot.validate_html_response(html, source="x", url="https://x.nl")

    def test_funda_human_verification_is_blocked_and_keeps_html(self):
        html = "<html><title>Je bent bijna op de pagina die je zoekt [funda]</title><body><p>Daarom moeten we soms verifiëren dat onze bezoekers echte mensen zijn.</p></body></html>"
        with self.assertRaises(bot.BlockedPageError) as raised:
            bot.validate_html_response(html, source="funda", url="https://funda.nl")
        self.assertEqual(getattr(raised.exception, "html"), html)

    @patch.object(bot.time, "sleep")
    def test_503_and_retry_after_are_retried(self, sleep):
        failed = Mock(status_code=503, headers={"Retry-After": "2"})
        failed.raise_for_status.side_effect = requests.HTTPError("503")
        good = Mock(status_code=200, headers={"Content-Type": "text/html"}, text="<html><body>Een geldige maar lege resultaatpagina met geen woningen gevonden</body></html>")
        session = Mock(); session.get.side_effect = [failed, good]
        with patch.object(bot, "SESSION", session):
            self.assertIn("geen woningen", bot.fetch("https://x.nl", "x"))
        sleep.assert_called_once_with(2.0)

    def test_run_health(self):
        healthy = bot.SourceResult("a", urls_attempted=1, requests_ok=1)
        failed = bot.SourceResult("b", urls_attempted=1, requests_failed=1)
        self.assertEqual(bot.calculate_run_status([healthy, failed]), "warning")
        self.assertEqual(bot.calculate_run_status([failed]), "failed")


class PhaseOneAndTwoTests(unittest.TestCase):
    FIXTURES = Path(__file__).parent / "fixtures"

    def fixture(self, name):
        return (self.FIXTURES / name).read_text(encoding="utf-8")

    def test_specialized_pararius_and_vesteda_availability(self):
        pararius = bot.extract_specialized_listings("pararius", "https://www.pararius.nl", bot.BeautifulSoup(self.fixture("pararius_results.html"), "lxml"))[0]
        vesteda = bot.extract_specialized_listings("vesteda", "https://www.vesteda.com", bot.BeautifulSoup(self.fixture("vesteda_results.html"), "lxml"))[0]
        self.assertEqual((pararius.availability, pararius.postcode), ("available", "3511 AA"))
        self.assertEqual(vesteda.availability, "reserved")

    def test_pagination_stops_when_page_has_no_new_urls(self):
        pages = [self.fixture("funda_results.html"), self.fixture("funda_page_2.html"), self.fixture("funda_page_2.html")]
        with patch.object(bot, "fetch", side_effect=pages) as fetch:
            result = bot.scrape_funda("funda", ["https://www.funda.nl/zoeken/huur?selected_area=utrecht"], {"max_pages": 5, "enrich_details": False})
        self.assertEqual(result.unique_listings, 2)
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("search_result=2", fetch.call_args_list[1].args[0])

    def test_detail_enrichment_fills_missing_fields(self):
        listing = bot.Listing("funda", "Teststraat 1 Utrecht", "https://www.funda.nl/detail/huur/utrecht/test/1")
        result = bot.SourceResult("funda")
        with patch.object(bot, "fetch", return_value=self.fixture("listing_detail.html")):
            enriched = bot.enrich_listing_details("funda", [listing], result, 1)[0]
        self.assertEqual((enriched.rent, enriched.area, enriched.rooms), (1750, 72, 3))
        self.assertEqual((enriched.availability, enriched.postcode), ("available", "3521 AA"))
        self.assertEqual(result.detail_requests, 1)

    def test_detail_request_budget_is_respected(self):
        listings = [bot.Listing("funda", f"Woning {number} Utrecht", f"https://x.nl/{number}") for number in range(3)]
        result = bot.SourceResult("funda")
        with patch.object(bot, "fetch", return_value=self.fixture("listing_detail.html")) as fetch:
            bot.enrich_listing_details("funda", listings, result, 1)
        self.assertEqual(fetch.call_count, 1)

    def test_utrecht_postcode_and_district_are_matches(self):
        filters = {"city": "Utrecht", "max_rent": 2000, "min_area": 60}
        postcode = bot.Listing("x", "Voorbeeld", "https://x.nl/1", rent=1500, area=70, availability="available", postcode="3541 AB")
        district = bot.Listing("x", "Woning Leidsche Rijn", "https://x.nl/2", rent=1500, area=70, availability="available")
        self.assertEqual(bot.classify_listing(postcode, filters), "match")
        self.assertEqual(bot.classify_listing(district, filters), "match")

    def test_missing_data_and_unknown_availability_are_possible(self):
        filters = {"city": "Utrecht", "max_rent": 2000, "min_area": 60}
        missing = bot.Listing("x", "Woning Utrecht", "https://x.nl/1", rent=1500)
        unknown = bot.Listing("x", "Woning Utrecht", "https://x.nl/2", rent=1500, area=70)
        rented = bot.Listing("x", "Woning Utrecht", "https://x.nl/3", rent=1500, area=70, availability="rented")
        self.assertEqual(bot.classify_listing(missing, filters), "possible_match")
        self.assertEqual(bot.classify_listing(unknown, filters), "possible_match")
        self.assertEqual(bot.classify_listing(rented, filters), "rejected")

    def test_utrecht_search_context_is_inherited(self):
        html = '<article><a href="/detail/huur/object/1">Teststraat 1</a><span>€ 1.500 per maand</span><span>70 m²</span></article>'
        listing = bot.extract_from_blocks("funda", "https://www.funda.nl/zoeken/huur?selected_area=utrecht", bot.BeautifulSoup(html, "lxml"))[0]
        self.assertEqual(listing.city, "Utrecht")

    def test_vbt_sapper_payload(self):
        html = r'''<script>__SAPPER__={houses:[{address:{city:"Utrecht",house:"Testlaan 10"},prices:{rental:{price:1650,type:"month"}},plot:72,rooms:3,acceptance:"2026-08-01",url:"\u002Fwoning\u002Futrecht-testlaan-10"}]}</script>'''
        listing = bot.extract_vbt_embedded("https://vbtverhuurmakelaars.nl/woningen", html)[0]
        self.assertEqual((listing.city, listing.rent, listing.area, listing.rooms), ("Utrecht", 1650, 72, 3))


class ConfigStateSummaryTests(unittest.TestCase):
    def valid_config(self):
        return {"filters": {"max_rent": 2000, "min_area": 0}, "sources": {"x": {"enabled": True, "urls": ["https://x.nl"]}}}

    def test_config_defaults_and_validation(self):
        cfg = bot.validate_config(self.valid_config())
        self.assertFalse(cfg["filters"]["allow_missing_rent"])
        bad = self.valid_config(); bad["filters"]["max_rent"] = 0
        with self.assertRaises(bot.ConfigurationError): bot.validate_config(bad)

    def test_legacy_state_migration_and_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"; path.write_text('["abc"]', encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path): self.assertEqual(bot.load_seen(), {"abc"})
            self.assertTrue(path.with_suffix(".json.v1.bak").exists())
            path.write_text("{broken", encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path), self.assertRaises(bot.StateError): bot.load_seen()

    def test_summary_noop_and_escapes(self):
        with patch.dict(os.environ, {}, clear=True):
            bot.write_github_summary([], Counter(), 0, "healthy")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            result = bot.SourceResult("evil|source", urls_attempted=1, requests_ok=1)
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}):
                bot.write_github_summary([result], Counter({"missing_rent": 2}), 0, "healthy")
            text = path.read_text(encoding="utf-8")
            self.assertIn("evil\\|source", text)
            self.assertIn("Prijs ontbreekt | 2", text)

    def test_debug_artifact_masks_secret_and_limits_html(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "DEBUG_DIR", Path(directory)):
            path = bot.write_debug_artifact("../Funda", "https://x.nl/?token=secretvalue", "fout", "token=supersecretvalue " + "x" * 120000)
            self.assertEqual(path.parent, Path(directory))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("secretvalue", json.dumps(payload))
            self.assertLessEqual(len(payload["html_fragment"]), 100000)

    def test_price_drop_notified_and_failed_send_keeps_old_price(self):
        old = bot.Listing("funda", "Woning Utrecht", "https://x.nl/woning/1", "Utrecht", 1700, 70)
        new = bot.Listing("funda", "Woning Utrecht", "https://x.nl/woning/1", "Utrecht", 1500, 70)
        record = {"source": "funda", "url": old.url, "title": old.title, "rent": 1700, "area": 70,
                  "first_seen_at": "2026-01-01T00:00:00+00:00", "last_seen_at": "2026-01-01T00:00:00+00:00", "last_notified_at": "2026-01-01T00:00:00+00:00"}
        state = {"version": 2, "listings": {old.uid: record}, "sources": {}}
        seen = bot.SeenState([old.uid], state)
        config = {"filters": {"city": "Utrecht", "max_rent": 2000, "min_area": 60}, "notifications": {"notify_on_price_drop": True}, "state": {"stale_after_days": 60}}
        with patch.object(bot, "scrape_all", return_value=[new]), patch.object(bot, "send_telegram", side_effect=RuntimeError("offline")), patch.object(bot, "save_seen"):
            with self.assertRaises(RuntimeError): bot.check_once(config, seen)
        self.assertEqual(state["listings"][old.uid]["rent"], 1700)

    def test_source_failure_notification_is_deduplicated(self):
        state = {"version": 2, "listings": {}, "sources": {}}
        seen = bot.SeenState([], state)
        result = bot.SourceResult("funda", urls_attempted=1, requests_failed=1, errors=["timeout"])
        config = {"notifications": {"notify_on_source_failure": True, "source_failure_repeat_hours": 12}}
        with patch.object(bot, "send_telegram") as send:
            bot.update_source_state(config, seen, [result])
            bot.update_source_state(config, seen, [result])
            bot.update_source_state(config, seen, [result])
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
