# Veille Journal

Journal de veille personnel. Vous définissez des **thèmes** (par exemple « Azure Cloud » ou
« Présidentielle 2027 ») ; chaque nuit, un worker collecte les actualités récentes (flux RSS
épinglés + recherche SearXNG auto-hébergée), écarte ce qui n'est pas pertinent, les résume
**en français** avec un modèle open source local (Ollama, `qwen3:8b` par défaut) et publie
**un article très court par thème et par jour** : un titre et une phrase par actualité, avec
les liens vers les sources.

Le site propose la page du jour, l'archive par date et par thème, et une recherche plein texte.

## Démarrage local (Windows + Docker Desktop)

```powershell
git clone https://github.com/KevinSiry/Veille-Journal.git
cd Veille-Journal
Copy-Item .env.example docker\.env
# Remplissez APP_PASSWORD_HASH, SESSION_SECRET et SEARXNG_SECRET : voir docs/SETUP.md
docker compose -f docker/compose.yml -f docker/compose.dev.yml up --build
```

Au premier démarrage, les modèles (environ 6 Go) sont téléchargés automatiquement. Le site
est ensuite sur <http://localhost:8080>. Pour générer les articles tout de suite :

```powershell
docker compose -f docker/compose.yml -f docker/compose.dev.yml exec worker veille run --force
```

Le guide complet, pas à pas (secrets, VPS, reverse proxy, publication d'une version) est dans
[`docs/SETUP.md`](docs/SETUP.md).

## Commandes

| Commande | Rôle |
|---|---|
| `veille serve` | démarre le site |
| `veille worker` | planificateur : génération chaque jour à `RUN_AT` (Europe/Paris) |
| `veille run [--force] [--theme ID]` | génère les articles du jour maintenant |
| `veille migrate` | applique les migrations de la base |
| `veille doctor` | vérifie la base, Ollama, les modèles et SearXNG |
| `veille hash-password` | produit le hash argon2id pour `APP_PASSWORD_HASH` |

## Développement

```powershell
uv sync
uv run ruff check ; uv run ruff format --check ; uv run mypy src ; uv run pytest --cov=veille
```

Documentation technique : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
prompts : [`docs/PROMPTS.md`](docs/PROMPTS.md).
