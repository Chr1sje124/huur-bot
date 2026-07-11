import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from bs4 import BeautifulSoup


class BotTests(unittest.TestCase):
    def listing(self, url="https://example.nl/woning/123?utm_source=test", rent=1500):
        return bot.Listing("TEST", "Woning Utrecht", url, "Utrecht", rent, 70, 3)

    def test_uid_is_stable_when_price_changes(self):
        self.assertEqual(self.listing(rent=1500).uid, self.listing(rent=1600).uid)

    def test_uid_ignores_tracking_and_fragment(self):
        a = self.listing("https://EXAMPLE.nl/woning/123/?utm_source=x#foto")
        b = self.listing("https://example.nl/woning/123")
        self.assertEqual(a.uid, b.uid)

    def test_failed_telegram_message_is_not_marked_seen(self):
        listing = self.listing()
        seen = set()
        config = {"filters": {"city": "Utrecht", "max_rent": 2000, "min_area": 60}}
        with patch.object(bot, "scrape_all", return_value=[listing]), \
             patch.object(bot, "send_telegram", side_effect=RuntimeError("offline")), \
             patch.object(bot, "save_seen"):
            with self.assertRaises(RuntimeError):
                bot.check_once(config, seen)
        self.assertNotIn(listing.uid, seen)

    def test_successful_message_is_saved(self):
        listing = self.listing()
        seen = set()
        config = {"filters": {"city": "Utrecht", "max_rent": 2000, "min_area": 60}}
        with patch.object(bot, "scrape_all", return_value=[listing]), \
             patch.object(bot, "send_telegram"), \
             patch.object(bot, "save_seen") as save:
            matches, sent = bot.check_once(config, seen)
        self.assertEqual((matches, sent), (1, 1))
        self.assertIn(listing.uid, seen)
        self.assertGreaterEqual(save.call_count, 1)

    def test_funda_only_extracts_real_advert_links(self):
        html = """
        <nav><a href='/zoeken/huur'>Huur zoeken voor € 1.000</a></nav>
        <article><a href='/detail/huur/utrecht/appartement-test/123/'>Teststraat 1 Utrecht</a>
        <span>€ 1.750 per maand</span><span>72 m²</span></article>
        """
        listings = bot.extract_from_blocks("funda", "https://www.funda.nl", BeautifulSoup(html, "lxml"))
        self.assertEqual(len(listings), 1)
        self.assertIn("/detail/huur/", listings[0].url)

    def test_vbt_ignores_project_link_and_keeps_woning(self):
        html = """
        <div class='card'><a href='/project/wonderwoods-utrecht'>Project</a></div>
        <div class='card woning'><a href='/woning/utrecht-teststraat-1'>Teststraat 1 Utrecht</a>
        <span>€ 1.900 p/m</span><span>80 m2</span></div>
        """
        listings = bot.extract_from_blocks("vbt", "https://vbtverhuurmakelaars.nl", BeautifulSoup(html, "lxml"))
        self.assertEqual(len(listings), 1)
        self.assertIn("/woning/", listings[0].url)


if __name__ == "__main__":
    unittest.main()
