# RSS: piotrkow.pl (Nasze miasto → Aktualności)

Źródło:
https://www.piotrkow.pl/nasze-miasto-t70/aktualnosci-a75

## Pliki
- `scraper.py` – generuje RSS
- `feed.xml` – wynik (commitowany do repo)
- `.github/workflows/rss.yml` – uruchamia co 2h oraz ręcznie

## Jak używać
1. Włącz Actions w repo (jeśli wyłączone).
2. Uruchom workflow ręcznie (Actions → RSS → Run workflow).
3. Po chwili w repo pojawi się/odświeży `feed.xml`.
