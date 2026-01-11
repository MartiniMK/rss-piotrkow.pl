# piotrkow.pl – RSS (Aktualności)

Repozytorium generuje plik RSS `docs/feed.xml` na podstawie linków z bloku **„Aktualności”** ze strony głównej piotrkow.pl.
Jest to obejście sytuacji, w której widok listy aktualności (`/nasze-miasto-t70/aktualnosci-a75/...`) nie zwraca już w HTML listy artykułów.

## Jak uruchomić lokalnie

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

pip install -r requirements.txt
python src/generate_feed.py --out docs/feed.xml --max-items 30
