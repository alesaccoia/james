# James

Dashboard marketing standalone: legge i dati sincronizzati da Airbyte (Facebook Marketing, Google Ads, GA4, ...) e li mostra riuniti in un'unica vista — spesa, impression, click, conversioni e KPI CRM per canale e per campagna.

Progetto indipendente, riutilizzabile per altri progetti/clienti: nessuna dipendenza da altre app.

## Setup locale

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Senza `DB_HOST` nel `.env`, il DB di default è sqlite locale (utile per sviluppo/anteprima).

## Configurazione (`.env`)

```
SECRET_KEY=...
DEBUG=false
ALLOWED_HOSTS=james-mentor.iside.systems
DB_HOST=<host del Postgres Airbyte>
DB_PORT=5432
DB_NAME=james
AIRBYTE_DB_NAME=airbyte_raw
DB_USER=airbyte_writer
DB_PASSWORD=...
```

## Import dati da Airbyte

Airbyte scrive tabelle tipizzate (Destinations V2) nel database `airbyte_raw`. Il comando le mirrora in `AirbyteRecord` (deduplicate per chiave naturale, non per id grezzo di Airbyte):

```bash
.venv/bin/python manage.py import_airbyte
```

Da lanciare manualmente dopo ogni sync Airbyte (nessun cron installato di default).

In produzione la sorgente Airbyte `Google Ads` sincronizza ogni sei ore gli stream `campaign`, `campaign_budget` e `ad_performance` verso le tabelle `gads_*` di `airbyte_raw`. JAMES non usa credenziali Google Ads e non interroga direttamente l'API: importa esclusivamente il landing Airbyte.

## Aggiungere un nuovo canale/stream

In `dashboard/views.py`, aggiungi una entry al dict `CHANNELS` con i nomi dei campi del nuovo stream. Se lo stream ha bisogno di una chiave di dedup diversa dall'id grezzo di Airbyte, aggiungila in `NATURAL_KEYS` dentro `dashboard/management/commands/import_airbyte.py`.
