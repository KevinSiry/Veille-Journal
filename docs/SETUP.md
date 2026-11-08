# Installation et déploiement de Veille Journal

Ce guide part de zéro. Les commandes sont à copier-coller telles quelles :

- **PowerShell** : poste de développement Windows (Docker Desktop) ;
- **bash** : serveur Linux (VPS) ou poste Linux/macOS.

Sommaire :

1. [Comment c'est organisé](#1-comment-cest-organisé)
2. [Démarrage en local (Windows)](#2-démarrage-en-local-windows)
3. [Déploiement sur le VPS](#3-déploiement-sur-le-vps)
4. [Publier une version](#4-publier-une-version)
5. [Exploitation : vérifier, revenir en arrière, sauvegarder](#5-exploitation)
6. [Dépannage](#6-dépannage)

---

## 1. Comment c'est organisé

La pile docker (`docker/compose.yml`) contient cinq services :

| Service       | Rôle                                                                 | Exposé ?                        |
|---------------|----------------------------------------------------------------------|---------------------------------|
| `web`         | le site (FastAPI), `veille serve`                                    | `127.0.0.1:8080` uniquement     |
| `worker`      | la génération nocturne, `veille worker`                              | non                             |
| `ollama`      | le modèle de langue local (résumés, embeddings)                      | non                             |
| `ollama-init` | tâche ponctuelle : télécharge les modèles au premier démarrage        | non                             |
| `searxng`     | le métamoteur de recherche privé (API JSON)                           | non                             |

Deux réseaux docker :

- `internal` (sans accès à Internet) : les échanges entre services (`web`/`worker` → `ollama`,
  `searxng`) ;
- `egress` (avec accès à Internet) : le `worker` lit les flux RSS et les pages, `searxng`
  interroge les moteurs de recherche, `ollama` télécharge les modèles, et `web` a besoin d'un
  réseau ordinaire pour publier son port.

Seul `web` publie un port, et seulement sur `127.0.0.1` : depuis l'extérieur, on passe
obligatoirement par le reverse proxy déjà présent sur le serveur.

**Convention unique pour le fichier `.env`** : il est toujours **à côté des fichiers
compose**.

- en local : `docker/.env` (copie de `.env.example`) ;
- sur le VPS : `$DEPLOY_PATH/.env` (par exemple `/opt/veille-journal/.env`).

Il n'est jamais commité (`.gitignore`).

> **Le piège du `$`.** Docker compose remplace les `$xxx` présents dans `.env`, **y compris
> pour les variables transmises aux conteneurs**. Le hash du mot de passe (`$argon2id$v=19$...`)
> doit donc être écrit **entre apostrophes simples** :
> `APP_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$...$...'`.
> Sans apostrophes, ou avec des guillemets doubles `"..."`, le hash arrive tronqué et la
> connexion échoue. (Autre solution : doubler chaque `$` en `$$`, sans apostrophes.)

---

## 2. Démarrage en local (Windows)

### 2.1 Prérequis

1. **Docker Desktop** 4.27 ou plus récent (Docker Compose ≥ 2.24.4, nécessaire pour
   `compose.dev.yml`) : <https://www.docker.com/products/docker-desktop/> (moteur WSL 2,
   option par défaut). Redémarrez si demandé, puis lancez Docker Desktop.
2. **Git** : <https://git-scm.com/download/win>.
3. Facultatif : **uv** (pour lancer les tests hors docker) :

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

4. Mémoire : le modèle `qwen3:8b` a besoin d'environ 6 Go. Donnez **au moins 12 Go** à WSL 2
   (voir [Dépannage](#docker-desktop-manque-de-mémoire)).

Vérifiez dans un nouveau PowerShell :

```powershell
docker version
docker compose version
git --version
```

### 2.2 Récupérer le code

```powershell
cd $HOME
git clone https://github.com/KevinSiry/Veille-Journal.git
cd Veille-Journal
```

(Le dépôt est privé : Git vous demandera de vous connecter à GitHub.)

### 2.3 Créer `docker/.env`

```powershell
Copy-Item .env.example docker\.env
notepad docker\.env
```

Remplissez **d'abord** les deux secrets aléatoires (la commande de hash de l'étape suivante en
a besoin). Pour générer une valeur, au choix :

```powershell
# avec docker (rien à installer)
docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_urlsafe(48))"

# ou en PowerShell pur
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)
```

Lancez la commande deux fois et collez les résultats dans :

- `SESSION_SECRET=...`
- `SEARXNG_SECRET=...`

(Sous Linux : `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` ou
`openssl rand -base64 48`.)

Ces valeurs sont vides dans `.env.example` : tant que `SESSION_SECRET` est vide, trop court,
peu aléatoire (moins de 12 caractères différents) ou ressemble à un texte d'exemple, le site
refuse de démarrer et le journal (`docker compose logs web`) indique comment le générer.
De même, `APP_PASSWORD_HASH` doit rester vide (connexion désactivée) ou contenir un hash qui
commence par `$argon2id$` (étape suivante).

### 2.4 Générer le hash du mot de passe

```powershell
docker compose -f docker/compose.yml -f docker/compose.dev.yml run --rm --no-deps web veille hash-password
```

La première fois, l'image est construite (quelques minutes). Tapez votre mot de passe
(8 caractères minimum, rien ne s'affiche) puis confirmez. La commande affiche une ligne qui
commence par `$argon2id$`. Copiez-la dans `docker\.env` **entre apostrophes simples** :

```dotenv
APP_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$xxxxxxxxxxxx$yyyyyyyyyyyyyyyy'
```

Avec uv installé, `uv run veille hash-password` donne le même résultat.

### 2.5 Lancer la pile

```powershell
docker compose -f docker/compose.yml -f docker/compose.dev.yml up --build
```

Au **premier démarrage**, `ollama-init` télécharge les modèles (environ 6 Go : `qwen3:8b`
puis `bge-m3`, en plus de l'image `ollama` d'environ 3,4 Go ; prévoyez **15 Go d'espace
disque libre**). Cela peut prendre de longues minutes ; le `worker` ne démarre qu'une fois les
modèles prêts, le site est disponible avant. Pour suivre le téléchargement depuis un second
PowerShell :

```powershell
docker compose -f docker/compose.yml -f docker/compose.dev.yml logs -f ollama-init
```

La ligne `modeles prets` signale la fin. Les modèles sont conservés dans le volume
`ollama-models` : les démarrages suivants sont rapides.

Astuce : ajoutez `-d` à `up` pour rendre la main (`... up --build -d`), puis
`docker compose -f docker/compose.yml -f docker/compose.dev.yml ps` pour l'état des services.

### 2.6 Utiliser le site

1. Ouvrez <http://localhost:8080> et connectez-vous avec votre mot de passe.
2. Créez un thème (nom, description, mots-clés, éventuellement des flux RSS).
3. Vérifiez la santé de l'ensemble (base, Ollama, modèles, SearXNG) ; cette commande
   fonctionne même si le `worker` n'a pas démarré :

   ```powershell
   docker compose -f docker/compose.yml -f docker/compose.dev.yml run --rm --no-deps worker veille doctor
   ```

4. Lancez une génération tout de suite, sans attendre la nuit :

   ```powershell
   docker compose -f docker/compose.yml -f docker/compose.dev.yml exec worker veille run --force
   ```

   Sur CPU, comptez plusieurs minutes par thème (mesuré : environ 30 s par actualité
   résumée sur 4 cœurs, soit 5 à 6 minutes pour un thème de 8 actualités).

Pour arrêter : `Ctrl+C` (ou `docker compose -f docker/compose.yml -f docker/compose.dev.yml down`).
Les données restent dans les volumes ; `down -v` efface **tout** (base et modèles).

---

## 3. Déploiement sur le VPS

Hypothèses : Linux avec Docker Engine et le plugin compose (`docker compose version`), un
reverse proxy déjà en place (Traefik ou Caddy), un nom de domaine pointant vers le serveur.

### 3.1 Utilisateur de déploiement et dossier

En `root` (ou avec `sudo`) sur le VPS :

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo usermod -aG docker deploy
sudo mkdir -p /opt/veille-journal/searxng
sudo chown -R deploy:deploy /opt/veille-journal
```

> Être membre du groupe `docker` équivaut à être administrateur de la machine : réservez cet
> utilisateur au déploiement.

### 3.2 Clé SSH de déploiement (sur votre poste)

```bash
ssh-keygen -t ed25519 -f veille_deploy -N "" -C "veille-journal deploy"
```

(En PowerShell : `ssh-keygen -t ed25519 -f veille_deploy -C "veille-journal deploy"`, puis
appuyez deux fois sur Entrée pour une clé sans phrase de passe.)

Installez la clé **publique** chez l'utilisateur `deploy` du VPS :

```bash
# sur le VPS, en root
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
echo "COLLEZ ICI LE CONTENU DE veille_deploy.pub" | sudo tee -a /home/deploy/.ssh/authorized_keys
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Testez depuis votre poste : `ssh -i veille_deploy deploy@VOTRE_SERVEUR docker ps`.

### 3.3 Fichier `.env` de production

Sur le VPS, en tant que `deploy` (`sudo -iu deploy`) :

```bash
cd /opt/veille-journal
nano .env
```

Collez le contenu de `.env.example`, puis :

- `SESSION_SECRET` et `SEARXNG_SECRET` : `openssl rand -base64 48` (deux valeurs différentes) ;
- `APP_PASSWORD_HASH` : générez-le sur votre poste (étape 2.4) ou sur le VPS une fois l'image
  disponible avec
  `docker run --rm -it ghcr.io/kevinsiry/veille-journal:latest veille hash-password`,
  **entre apostrophes simples** ;
- `COOKIE_SECURE=true` (le site est servi en HTTPS par le proxy) ;
- `FORWARDED_ALLOW_IPS=172.16.0.0/12` : adresse(s) du reverse proxy autorisées à transmettre
  l'IP réelle du visiteur (voir 3.5) ;
- `WEB_PORT=8080` ou un autre port libre si 8080 est déjà pris par une autre application.

```bash
chmod 600 .env
```

### 3.4 Accès au registre d'images (dépôt privé)

L'image `ghcr.io/kevinsiry/veille-journal` est privée. Créez un **classic personal access
token** sur <https://github.com/settings/tokens> avec uniquement la permission
`read:packages`, puis, en tant que `deploy` sur le VPS :

```bash
echo "VOTRE_TOKEN" | docker login ghcr.io -u KevinSiry --password-stdin
```

### 3.5 Brancher le reverse proxy existant

Le service écoute sur `127.0.0.1:${WEB_PORT}` (8080 par défaut). Le serveur web (uvicorn)
est lancé avec `--proxy-headers` et ne fait confiance aux en-têtes `X-Forwarded-For` /
`X-Forwarded-Proto` que s'ils viennent d'une adresse listée dans `FORWARDED_ALLOW_IPS`
(`127.0.0.1` si la variable est absente). C'est ce qui permet à la limite de tentatives de
connexion de voir la vraie adresse IP du visiteur ; ne mettez **jamais** `*`, sinon n'importe
qui pourrait falsifier son adresse et contourner cette limite.

Le site tourne dans un conteneur : le proxy n'y arrive pas depuis `127.0.0.1` mais depuis un
réseau docker.

- Proxy installé sur la machine (Caddy, nginx) qui joint `127.0.0.1:8080` : le conteneur voit
  la passerelle du réseau docker (`172.17.0.1`, `172.18.0.1`...).
- Proxy dans un conteneur (Traefik, Caddy) sur un réseau docker partagé avec `web` : le
  conteneur voit l'adresse du proxy sur ce réseau (`172.x.y.z`).

Dans les deux cas, la valeur recommandée est `FORWARDED_ALLOW_IPS=172.16.0.0/12` (plages des
réseaux docker par défaut). Pour être plus strict, indiquez l'adresse exacte, lue avec
`docker network inspect <réseau>` (champ `Gateway`, ou `IPv4Address` du conteneur du proxy).
Après modification : `docker compose up -d web`.

**Caddy** — voir `docker/Caddyfile.example` : ajoutez le bloc à votre Caddyfile, remplacez
`veille.example.com`, puis rechargez Caddy (`sudo systemctl reload caddy`, ou
`docker exec <conteneur-caddy> caddy reload --config /etc/caddy/Caddyfile`).

- Caddy installé sur la machine : `reverse_proxy 127.0.0.1:8080` (option A) ;
- Caddy dans un conteneur : option B (`reverse_proxy web:8080`), en rattachant `web` au réseau
  docker de Caddy comme dans l'exemple Traefik ci-dessous.

**Traefik** — voir `docker/compose.traefik.example.yml` :

```bash
cd /opt/veille-journal
docker network ls     # repérez le réseau de Traefik
# copiez le fichier d'exemple depuis le dépôt (scp ou copier-coller) sous le nom compose.traefik.yml
nano compose.traefik.yml   # domaine, réseau, certresolver, entrypoint
echo "COMPOSE_FILE=compose.yml:compose.traefik.yml" >> .env
```

Grâce à `COMPOSE_FILE`, toutes les commandes `docker compose` lancées dans
`/opt/veille-journal` (y compris celles du déploiement automatique) incluent l'intégration
Traefik.

### 3.6 Secrets GitHub pour le déploiement automatique

Sur votre poste, avec la CLI GitHub (`gh auth login` une fois) :

```bash
gh secret set VPS_HOST --repo KevinSiry/Veille-Journal --body "IP_OU_NOM_DU_SERVEUR"
gh secret set VPS_USER --repo KevinSiry/Veille-Journal --body "deploy"
gh secret set DEPLOY_PATH --repo KevinSiry/Veille-Journal --body "/opt/veille-journal"
gh secret set VPS_SSH_KEY --repo KevinSiry/Veille-Journal < veille_deploy
# facultatif : port SSH (22 par défaut)
gh secret set VPS_PORT --repo KevinSiry/Veille-Journal --body "22"
# recommandé : épingler la clé du serveur (sinon elle est acceptée au premier contact)
ssh-keyscan -p 22 IP_OU_NOM_DU_SERVEUR > known_hosts_vps
gh secret set VPS_KNOWN_HOSTS --repo KevinSiry/Veille-Journal < known_hosts_vps
```

En PowerShell, remplacez `< fichier` par `Get-Content fichier -Raw | gh secret set NOM --repo KevinSiry/Veille-Journal`.

Supprimez ensuite la clé privée de votre poste si vous n'en avez plus besoin
(`veille_deploy`), elle est conservée chiffrée par GitHub.

Facultatif : dans *Settings → Environments*, créez l'environnement `production` (il est créé
automatiquement sinon) et ajoutez une règle *Required reviewers* pour valider chaque
déploiement à la main. Les secrets peuvent aussi être rangés dans cet environnement.

Tant que ces secrets n'existent pas, la publication d'une version construit et publie l'image
mais **saute le déploiement** (« déploiement ignoré : secrets VPS non configurés »).

---

## 4. Publier une version

La CI (`.github/workflows/ci.yml`) tourne sur chaque pull request et chaque push sur `main` :
ruff, mypy, pytest (couverture ≥ 80 %) puis construction de l'image (sans publication).

Pour publier, depuis `main` à jour :

```bash
git tag v0.1.0
git push origin v0.1.0
```

Le workflow `.github/workflows/release.yml` :

1. relance les tests ;
2. construit et publie `ghcr.io/kevinsiry/veille-journal:0.1.0` et `:latest` ;
3. crée la release GitHub avec des notes générées ;
4. déploie sur le VPS : copie `compose.yml` et `searxng/settings.yml` dans `DEPLOY_PATH`,
   `docker compose pull` puis `up -d` avec `IMAGE_TAG=0.1.0`, attend que `/healthz` réponde
   (2 minutes maximum), puis note la version dans `DEPLOY_PATH/.image_tag`.
   **En cas d'échec, la version précédente est redéployée automatiquement** et le workflow
   est marqué en échec.

Suivez l'exécution dans l'onglet *Actions* du dépôt (ou `gh run watch --repo KevinSiry/Veille-Journal`).

---

## 5. Exploitation

Toutes les commandes suivantes se lancent sur le VPS, dans le dossier de déploiement :

```bash
cd /opt/veille-journal
export IMAGE_TAG="$(cat .image_tag)"   # la version actuellement déployée
```

> Sans `IMAGE_TAG`, compose utilise la valeur du `.env` (`latest`). Exportez-la comme
> ci-dessus avant toute commande `docker compose` manuelle.

### Vérifier

```bash
docker compose ps
docker compose logs --tail 100 web worker
docker compose run --rm --no-deps worker veille doctor
curl -fsS http://127.0.0.1:8080/healthz && echo OK
```

Sans session, `/healthz` ne renvoie que `{"status": ..., "db": ...}` (code 200 ou 503) ; la
version et la dernière exécution ne sont visibles qu'une fois connecté (ou sur `/admin/runs`).

Lancer une génération à la main : `docker compose exec worker veille run --force`.

### Revenir à une version précédente

```bash
export IMAGE_TAG=0.1.0          # la version voulue
docker compose pull && docker compose up -d --remove-orphans
echo "$IMAGE_TAG" > .image_tag
```

### Sauvegarder la base

La base SQLite est dans le volume docker `veille_veille-data`.

Sauvegarde à chaud (le site reste en ligne ; API de sauvegarde de SQLite) :

```bash
docker compose exec -T web python -c "import sqlite3; s = sqlite3.connect('/data/veille.db'); d = sqlite3.connect('/data/veille-backup.db'); s.backup(d); d.close()"
docker compose cp web:/data/veille-backup.db "./veille-$(date +%F).db"
docker compose exec -T web python -c "import os; os.remove('/data/veille-backup.db')"
```

Sauvegarde à froid (copie complète du volume, pile arrêtée) :

```bash
docker compose stop web worker
docker run --rm -v veille_veille-data:/data:ro -v "$PWD":/backup alpine \
  tar czf "/backup/veille-data-$(date +%F).tgz" -C /data .
docker compose start web worker
```

Restaurer une sauvegarde à froid :

```bash
docker compose stop web worker
docker run --rm -v veille_veille-data:/data -v "$PWD":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/veille-data-AAAA-MM-JJ.tgz -C /data && chown -R 10001:10001 /data"
docker compose start web worker
```

Pensez à copier ces fichiers hors du serveur (le volume `ollama-models` n'a pas besoin d'être
sauvegardé : les modèles se re-téléchargent).

---

## 6. Dépannage

### Fins de ligne Windows (CRLF)

Le dépôt contient un `.gitattributes` qui force les fins de ligne `LF` : les scripts et
fichiers de configuration restent valides dans les conteneurs Linux. Si vous aviez cloné avant
ce fichier, ou si un éditeur a converti des fichiers :

```powershell
git rm --cached -r -q . ; git reset --hard
```

(Attention : `git reset --hard` efface les modifications non commitées : commitez-les avant.)

### Docker Desktop manque de mémoire

Symptômes : `ollama` redémarre en boucle, erreurs « out of memory », machine très lente.
Créez ou modifiez `%UserProfile%\.wslconfig` :

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

```ini
[wsl2]
memory=12GB
```

Puis `wsl --shutdown` et redémarrez Docker Desktop.

### Le téléchargement des modèles est lent ou bloqué

- suivez-le : `docker compose -f docker/compose.yml -f docker/compose.dev.yml logs -f ollama-init` ;
- s'il échoue (coupure réseau), relancez simplement la pile : les téléchargements reprennent
  où ils en étaient ;
- `ollama-init` en erreur bloque le démarrage du `worker` (c'est voulu) : le site reste
  accessible.

### Ollama est occupé / les résumés expirent

Ollama traite une requête à la fois (`OLLAMA_NUM_PARALLEL=1`). Sur CPU, une génération prend
du temps et une requête lancée depuis le site pendant la génération nocturne attend son tour.
Si les journaux montrent des délais dépassés, augmentez `LLM_TIMEOUT_S` dans `.env` (par
exemple `300`), puis `docker compose up -d`. Vérifiez aussi `docker stats` : `ollama` est
limité à 6 processeurs et 12 Go (`OLLAMA_CPUS`, `OLLAMA_MEM_LIMIT` dans `.env`).

### « range of CPUs is from 0.01 to N »

La limite `OLLAMA_CPUS` (6 par défaut) dépasse le nombre de processeurs de la machine.
En local, `compose.dev.yml` supprime cette limite ; sur un serveur, réglez `OLLAMA_CPUS` dans
`.env` (par exemple `OLLAMA_CPUS=3.0`).

### La connexion refuse le bon mot de passe

Le hash a presque toujours été abîmé par le `$` : vérifiez que `APP_PASSWORD_HASH` est entre
apostrophes simples dans `.env`, puis recréez les conteneurs (`docker compose up -d`, un
simple `restart` ne relit pas `.env`). En local, vérifiez aussi que `COOKIE_SECURE=false` est
bien appliqué (c'est le cas avec `compose.dev.yml`).

### Le port 8080 est déjà utilisé

Sur le VPS, changez `WEB_PORT` dans `.env` (et dans la configuration du proxy). En local,
mettez par exemple `WEB_PORT=8081` dans `docker/.env`, relancez la pile et ouvrez
<http://localhost:8081>.
