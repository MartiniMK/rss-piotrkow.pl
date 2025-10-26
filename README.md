# RSS dla Piotrkow.pl (zbiorczy)

Automatycznie generowany kanał RSS łączący:
- Aktualności – Miasto: `https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75`
- Aktualności – Gospodarka: `https://www.piotrkow.pl/gospodarka-t71/aktualnosci-a107`
- Aktualności – Kultura i edukacja: `https://www.piotrkow.pl/kultura-i-edukacja-t72/aktualnosci-a108`
- Aktualności – Sport i turystyka: `https://www.piotrkow.pl/sport-i-turystyka-t73/aktualnosci-a109`

Feed: **`feed.xml`** (generowany co godzinę przez GitHub Actions).

## Jak to działa
1. `scraper.py` pobiera listy artykułów z ww. stron, odwiedza każdy artykuł, a następnie buduje `feed.xml`.
2. Workflow z `.github/workflows/rss.yml` uruchamia się co godzinę (lub ręcznie) i:
   - instaluje zależności z `requirements.txt`,
   - odpala `scraper.py`,
   - commituje i pushuje zaktualizowany `feed.xml`.

## Konfiguracja GitHub Pages
1. Settings → **Pages** → Source: `GitHub Actions` **lub** `Deploy from a branch` (jeśli wolisz).
2. Docelowy URL feeda będzie zwykle:
