# Operation Black Grid — FastAPI Edition

Tabletop platforma za simulaciju sajber kriza.

## Lokalni start

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# Otvori: http://localhost:8000
```

## Azure App Service deploy

1. Kreiraj **Linux Web App** (Python 3.11)
2. Postavi **Application Settings**:
   ```
   PARTICIPANT_CODE_HASH = sha256("vaš-kod-vežbe")
   MOD_PWD_HASH          = sha256("vaša-mod-lozinka")
   BLACKGRID_DB          = /home/data/blackgrid.db
   SCM_DO_BUILD_DURING_DEPLOYMENT = true
   ```
3. **Startup Command**: `bash startup.sh`
4. Deploy ZIP-om ili GitHub Actions

## Generisanje hashova

```python
import hashlib
print(hashlib.sha256("moj-kod".encode()).hexdigest())
```

## Struktura

```
blackgrid/
  main.py           ← FastAPI backend + sva logika
  requirements.txt
  startup.sh        ← Azure startup
  templates/
    base.html       ← Design system + layout
    index.html      ← Login stranica
    play.html       ← Učesnik panel
    mod.html        ← Moderator control panel
  static/           ← (opcionalno — slike, dodatni CSS)
```

## Timovi i faze

- **6 timova**: Izvršni bord, Pravni tim, IT/CERT, PR tim, Policija/Tužilaštvo, Diplomatija
- **3 faze**: Blackout/ransomware, CEO fraud/deepfake, Eksfiltracija podataka
- Svaki tim dobija 1 zajedničko + 3 tima-specifična pitanja po fazi

## Flow vežbe

1. Moderator se prijavi → `/mod` (autentikacija preko HttpOnly cookie-ja; token se više ne pojavljuje u URL-u)
2. Učesnici se prijave sa kodom vežbe → `/play` (cookie). Legacy linkovi `?token=BG-XXXXXXXX` i dalje rade — token se odmah uklanja iz adresne trake.

> **Napomene o bezbednosti i radu (v2):**
> - Aplikacija **mora** raditi sa tačno **1 workerom** (`startup.sh`) — sav state je u memoriji procesa; više workera dovodi do gubitka odgovora.
> - Observer link koristi **odvojeni observer ključ** (ne moderatorski token) — bezbedno za deljenje publici/projektoru.
> - Rate limit za login čita `X-Forwarded-For` (realan IP klijenta iza Azure front-enda).
> - PDF izveštaj koristi bundlovan DejaVu font (`static/fonts/`) — podržava srpsku latinicu i ćirilicu.
> - Izmene scenarija kroz moderatorski editor se čuvaju u SQLite i preživljavaju restart.
3. Moderator pokreće injecte i prati timove u realnom vremenu
4. Po završetku konsenzusa → moderator klikne "Otključaj" za sledeću fazu
