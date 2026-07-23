import unittest

from deal_radar.market_sources import (
    BuycycleMarketSource,
    KleinanzeigenMarketSource,
    MarktplaatsMarketSource,
)


class MarketSourceParserTest(unittest.TestCase):
    def test_kleinanzeigen_parser(self) -> None:
        page = b'''<article class="aditem" data-adid="123" data-href="/s-anzeige/trek/123-217-1">
        <script type="application/ld+json">{"title":"Trek Marlin 7 Gen 3 2025","description":"Gebrauchtes Mountainbike","contentUrl":"https://img.example/bike.jpg"}</script>
        <div class="aditem-main--top--left">10115 Berlin</div>
        <p class="aditem-main--middle--price-shipping--price">650 EUR VB</p></article>'''
        items = KleinanzeigenMarketSource.parse(page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_listing_id, "123")
        self.assertEqual(items[0].price_original, 650)
        self.assertEqual(items[0].currency, "EUR")
        self.assertEqual(items[0].country, "DE")

    def test_marktplaats_parser_deduplicates_responsive_markup(self) -> None:
        card = '''<li class="hz-Listing hz-Listing--list-item"><a href="/v/fietsen/m123-bike">
        <span class="ListingTitle_hash">Trek Marlin 7 Gen 3 2025</span>
        <span class="ListingDescription_hash">Goed onderhouden mountainbike</span>
        <h5 class="ListingPrice_hash">EUR 525,00</h5>
        <span class="hz-Listing-seller-name-new">Seller</span>
        <span data-testid="location-label">Utrecht</span><img src="https://img.example/bike.jpg"/></a></li>'''
        items = MarktplaatsMarketSource.parse((card + card).encode())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price_original, 525)
        self.assertEqual(items[0].source_listing_id, "123")
        self.assertEqual(items[0].country, "NL")

    def test_buycycle_parser(self) -> None:
        page = b'''<div data-cnstrc-item-id="77" data-cnstrc-item-name="Trek Marlin 7 Gen 3 2025" data-cnstrc-item-price="710">
        <a href="/en-us/product/trek-marlin-7"><img src="https://d1mgeijqpfaspl.cloudfront.net/bike.webp"/></a></div>'''
        items = BuycycleMarketSource.parse(page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price_original, 710)
        self.assertEqual(items[0].country, "EU")


if __name__ == "__main__":
    unittest.main()
