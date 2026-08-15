# Golf Search Belek

Echtzeit-Preisvergleich für Golf-Hotels in Belek (Antalya, Türkei).

Die App fragt bei einer Suche live die Preise von zwei Quellen ab
(Bilyana Golf und Golfbelek), vergleicht sie und zeigt dem Kunden den
günstigeren Preis abzüglich einer Reseller-Marge an. Wenn keine Quelle
einen Preis liefert, erscheint stattdessen ein Anfrage-Formular, dessen
Einsendungen in einer lokalen SQLite-Datenbank landen.

## Was drin ist

| Datei / Ordner | Inhalt |
| --- | --- |
| `app.py` | Komplette Anwendung: FastAPI-Server, beide Scraper, Preislogik, Cache, Anfrage-Formular |
| `templates/index.html` | Frontend (Suchmaske, Ergebnisliste, Formular) |
| `static/app.js` | Frontend-Logik |
| `static/style.css` | Styling |
| `static/hotel-images/` | Hotelbilder, nach Hotel-ID sortiert |
| `scripts/download_hotel_images.py` | Holt und optimiert die Hotelbilder neu |
| `data/hotel_images.json` | Zuordnung Hotel zu Bild |
| `requirements.txt` | Exakte Paketversionen |
| `golf-search.service` | Vorlage für den Dauerbetrieb per systemd |

Die Hotelliste (Namen, Sterne und die IDs bei beiden Quellen) steht direkt
in `app.py` unter `BELEK_HOTELS`.

## Installation

Getestet mit Python 3.12 unter Ubuntu.

```bash
git clone https://github.com/aliecommerce123-web/golf-search.git
cd golf-search

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Browser für das Scraping installieren (Pflicht, siehe unten)
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium
```

Der zweite Playwright-Befehl braucht Root-Rechte, weil er Systembibliotheken
nachinstalliert. Falls das nicht erlaubt ist, hilft `sudo` davor.

## Starten

```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8917
```

Danach im Browser `http://SERVER-IP:8917` öffnen.

Zum Testen auf dem eigenen Rechner geht auch einfach `.venv/bin/python app.py`.

## Dauerbetrieb per systemd

`golf-search.service` ist eine Vorlage. Darin `/PFAD/ZUM/PROJEKT` durch den
echten Pfad und `DEIN_USER` durch den Benutzernamen ersetzen, dann:

```bash
sudo cp golf-search.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now golf-search
```

Logs ansehen mit `journalctl -u golf-search -f`.

## Warum Playwright nötig ist

Eine der beiden Preisquellen baut ihre Preistabelle erst per JavaScript im
Browser auf. Ein einfacher Seitenabruf liefert dort eine leere Tabelle.
Deshalb startet die App im Hintergrund einen unsichtbaren Chromium, lädt die
Seite darin fertig und liest erst dann die Preise aus. Der Browser wird einmal
gestartet und für alle weiteren Abfragen wiederverwendet.

Ohne den Schritt `playwright install chromium` startet die App zwar, findet
aber bei dieser Quelle nie einen Preis.

## Datenbank

`data/golf_search.db` wird beim ersten Start automatisch angelegt, mit zwei
Tabellen:

- `inquiries` — die abgeschickten Anfrage-Formulare
- `proxy_cache` — zwischengespeicherte Preisabfragen, fünf Minuten gültig

Die Datei liegt bewusst nicht im Repository, weil dort Kundendaten drin
stehen. Fehlende Spalten werden bei jedem Start automatisch ergänzt, eine
alte Datenbank kann also einfach weiterverwendet werden.

## Hotelbilder erneuern

```bash
.venv/bin/python scripts/download_hotel_images.py
```

Das Skript lädt die Bilder neu, verkleinert sie und aktualisiert
`data/hotel_images.json`.

## Preislogik

Der angezeigte Kundenpreis ist immer fünf Prozent günstiger als der
günstigste gefundene Quellpreis. Der Wert steht in `app.py` als
`RESELLER_DISCOUNT`.

Nach außen werden die Namen der beiden Quellen nie ausgegeben, im Frontend
und in der API erscheinen nur die neutralen Kürzel `A` und `B`. Erst beim
Speichern einer Anfrage löst der Server das Kürzel intern wieder in den
echten Namen auf, damit nachvollziehbar bleibt, über welchen Anbieter
gebucht werden muss.
