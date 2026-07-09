# Utrecht Huur Bot

Bot die elke minuut huursites controleert en Telegram-meldingen stuurt voor woningen die voldoen aan:

- Plaats: Utrecht
- Max huur: €2.000 p/m
- Minimaal oppervlak: 60 m²
- Geen dubbele meldingen
- Directe Telegram-notificatie met link

## Installatie

1. Pak deze map uit.
2. Kopieer `config.example.yaml` naar `config.yaml`.
3. Vul je Telegram `token` en `chat_id` in.
4. Open CMD/Terminal in deze map.
5. Installeer pakketten:

```bash
pip install -r requirements.txt
```

6. Start de bot:

```bash
python bot.py
```

## Belangrijk

Scraping kan breken als websites hun HTML wijzigen of anti-botmaatregelen gebruiken. De bot is daarom modulair en logt fouten per bron zonder meteen te stoppen.

## Testmelding

Run:

```bash
python bot.py --test-telegram
```

## Eenmalig checken

Run:

```bash
python bot.py --once
```

## Alleen toekomstige nieuwe woningen melden

Zet in `config.yaml`:

```yaml
notify_existing_on_first_run: false
```

De bot markeert bestaande matches dan als gezien tijdens de eerste run en meldt pas nieuwe matches daarna.
