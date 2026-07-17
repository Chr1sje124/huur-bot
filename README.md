# Utrecht Huur Bot

Deze Python 3.12-bot controleert huurwoningbronnen, filtert advertenties en stuurt nieuwe matches via Telegram. GitHub Actions is de primaire runtime. Een bronfout stopt andere bronnen niet, maar een run waarin alle actieve bronnen technisch mislukken eindigt wel rood.

## Architectuur en diagnose

`bot.py` bevat een scraperregistry. Funda, Pararius en Vesteda gebruiken eigen kaartselectors en paginaregels; JSON-LD blijft een aanvullend extractiekanaal. Overige aanbieders gebruiken `scrape_generic`. Een `SourceResult` registreert per bron: geprobeerde, geslaagde en mislukte requests, detailrequests, kandidaten, unieke advertenties, beschikbaarheid, prijs-/oppervlaktedekking, complete records, exacte en mogelijke matches, blokkering en fouten.

De hoofdbronnen volgen configureerbaar meerdere resultaatpagina's en stoppen zodra een pagina geen nieuwe canonical URL's bevat. Kandidaten met ontbrekende prijs, oppervlakte of beschikbaarheid kunnen daarna gericht worden verrijkt via hun detailpagina. `max_detail_requests` begrenst dit extra verkeer.

Statussen:

- `healthy`: requests zijn verwerkt; nul aanbod is toegestaan wanneer de pagina dat plausibel aangeeft.
- `partial`: ten minste één request werkte en ten minste één request faalde.
- `parsing_warning`: kandidaten bestaan, maar minder dan 10% bevat zowel prijs als oppervlakte.
- `blocked`: captcha, Cloudflare-, access-denied- of cookiemuursignalen gevonden.
- `failed`: alle requests van de bron faalden.

De run is gezond als alle bronnen gezond zijn, een waarschuwing bij gedeeltelijke uitval of parseproblemen, en mislukt wanneer geen enkele bron technisch bruikbaar verwerkt is. Een rustige markt en nul filtermatches zijn dus geen fout.

## Installeren, testen en uitvoeren

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python bot.py --once
```

`python bot.py` behoudt de lokale intervalmodus. `python bot.py --test-telegram` stuurt één testbericht. Tests gebruiken mocks en fixtures en benaderen geen live sites.

## Configuratie

Telegramgegevens horen in `TELEGRAM_TOKEN` en `TELEGRAM_CHAT_ID` (GitHub Secrets), niet in Git. De relevante veilige defaults zijn:

```yaml
filters:
  city: Utrecht
  max_rent: 2000
  min_area: 60
  allow_missing_rent: false
  allow_missing_area: false

notifications:
  send_no_new_summary: false
  notify_on_source_failure: true
  notify_on_recovery: true
  source_failure_repeat_hours: 12
  notify_on_price_drop: true
  notify_on_price_increase: false
  send_possible_matches: true

state:
  stale_after_days: 60
```

Per bron zijn daarnaast beschikbaar:

```yaml
sources:
  funda:
    enabled: true
    max_pages: 3
    enrich_details: true
    max_detail_requests: 10
    urls:
      - "https://www.funda.nl/zoeken/huur?selected_area=..."
```

De classificatie heeft drie uitkomsten:

- `match`: locatie, prijs, oppervlakte en beschikbaarheid voldoen aantoonbaar;
- `possible_match`: prijs, oppervlakte of beschikbaarheid is nog onbekend;
- `rejected`: aantoonbaar verkeerde locatie, te duur, te klein, gereserveerd of verhuurd.

`send_possible_matches` bepaalt of mogelijke matches met een duidelijke waarschuwing naar Telegram gaan. Utrecht wordt herkend via plaats, configureerbare wijken/plaatsnamen en Utrechtse postcodeprefixes. De oude sleutel `send_summary_when_no_new` blijft werken; `notifications.send_no_new_summary` heeft voorrang. Bron-URL's, pagination/detailbudgetten, booleans, maximale huur, minimale oppervlakte en interval worden gevalideerd.

## State en meldingen

`seen.json` wordt atomisch geschreven. De oude lijst wordt automatisch gelezen en vóór de eerste schema-overgang bewaard als `seen.json.v1.bak`; bestaande IDs gaan niet verloren. De UID gebruikt bron plus canonical URL en blijft stabiel bij prijswijzigingen. State wordt pas na een geslaagde Telegrammelding bijgewerkt, zodat een Telegramfout geen woning als gemeld markeert.

`notifications.notify_on_price_drop` en `notify_on_price_increase` leggen de gewenste prijssemantiek vast. Foutmeldingen gebruiken bronstatus en herhaalinterval; herstelmeldingen zijn afzonderlijk configureerbaar. Een no-new-summary staat standaard bij voorkeur uit om heartbeatspam te voorkomen.

## GitHub Actions

De workflow draait op verschoven vijfminutenmomenten (`2-59/5`), is handmatig startbaar via **Actions → Utrecht Huur Bot → Run workflow**, cachet pip, compileert Python, draait tests, voert één botrun uit en commit alleen een gewijzigd `seen.json`. `concurrency` voorkomt gelijktijdige statewrites. GitHub Actions cron is best-effort en kan vertragen.

Elke run schrijft een tabel en afwijsredenen naar `$GITHUB_STEP_SUMMARY`. Buiten Actions doet die functie niets. Bij blokkering, technische uitval, onverwacht nul kandidaten of zeer lage datacompleetheid verschijnt beperkte, gemaskeerde diagnose in `debug/`; Actions uploadt uitsluitend die map zeven dagen als artifact. Telegramtokens, authorizationheaders en gevoelige querywaarden worden niet opgenomen.

## Een bron toevoegen

1. Voeg een advertentie-linkpatroon toe aan `SOURCE_LINK_PATTERNS`.
2. Maak zo nodig een kleine bronspecifieke scraper en registreer die in `SCRAPERS`.
3. Voeg kleine lokale HTML-fixtures en extractie-/healthtests toe.
4. Voeg de bron met één of meer zoek-URL's toe aan `config.yaml`.

Scrapers omzeilen geen captcha's of toegangscontrole. Websites kunnen hun HTML wijzigen; fixturetests bewijzen parsergedrag, niet dat een live site op elk moment bereikbaar is.

Voor WoningNet Regio Utrecht, Woonin en Bo-Ex staan voorbereidende bronprofielen in `config.yaml`. Ze zijn bewust standaard uitgeschakeld totdat hun actuele publieke zoekpagina, eventuele login en toewijzingsvoorwaarden handmatig zijn gevalideerd. De al actieve directe verhuurders krijgen wel detailverrijking binnen een klein requestbudget.

Historische anomaliedetectie, HTTP-responsecaching en verdere interactieve Telegrambediening behoren tot fase 3 en zijn nog niet toegevoegd.
