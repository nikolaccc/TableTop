"""
Operation Black Grid — FastAPI edition
Single-file backend ready for Azure App Service.
"""

import os, json, time, uuid, hashlib, threading, sqlite3, atexit, random
from datetime import datetime
from functools import wraps
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ─── OPTIONAL PDF ────────────────────────────────────────────────────────────
try:
    from fpdf import FPDF
    PDF_OK = True
except ImportError:
    PDF_OK = False

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG & ACCESS CONTROL
# ═══════════════════════════════════════════════════════════════════════════════

_PARTICIPANT_HASH = os.environ.get("PARTICIPANT_CODE_HASH", "").strip()
_MOD_HASH         = os.environ.get("MOD_PWD_HASH", "").strip()

def _sha(v: str) -> str:
    return hashlib.sha256(str(v).strip().encode()).hexdigest()

def check_participant(code: str) -> bool:
    return bool(_PARTICIPANT_HASH) and _sha(code) == _PARTICIPANT_HASH

def check_mod(pw: str) -> bool:
    return bool(_MOD_HASH) and _sha(pw) == _MOD_HASH

# ─── RATE LIMITER ─────────────────────────────────────────────────────────────
_RATE: dict = {}
_RATE_MAX  = int(os.environ.get("BLACKGRID_RATE_MAX", "30"))
_RATE_WIN  = 60
_RATE_LOCK = threading.Lock()

def rate_ok(ip: str) -> bool:
    if _RATE_MAX <= 0:
        return True
    now = time.time()
    with _RATE_LOCK:
        ts = [t for t in _RATE.get(ip, []) if now - t < _RATE_WIN]
        if len(ts) >= _RATE_MAX:
            _RATE[ip] = ts
            return False
        ts.append(now)
        _RATE[ip] = ts
        return True

# ═══════════════════════════════════════════════════════════════════════════════
#  PERSISTENCE — SQLite
# ═══════════════════════════════════════════════════════════════════════════════

_DB_DIR  = "/home/data" if os.path.isdir("/home/data") else "/tmp"
DB_PATH  = os.environ.get("BLACKGRID_DB", os.path.join(_DB_DIR, "blackgrid.db"))
STATE_LOCK = threading.RLock()

def _db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    with _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("""CREATE TABLE IF NOT EXISTS state_blob(
            key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)""")
        c.commit()

def db_save():
    try:
        with STATE_LOCK:
            payload = json.dumps({k: STATE[k] for k in STATE}, default=str)
        with _db() as c:
            c.execute("INSERT OR REPLACE INTO state_blob VALUES(?,?,?)",
                      ("snap", payload, time.time()))
            c.commit()
    except Exception as e:
        print(f"[DB save] {e}")

def db_load():
    try:
        with _db() as c:
            row = c.execute("SELECT value FROM state_blob WHERE key=?", ("snap",)).fetchone()
        if row:
            data = json.loads(row["value"])
            with STATE_LOCK:
                for k, v in data.items():
                    if k in STATE:
                        STATE[k] = v
    except Exception as e:
        print(f"[DB load] {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  GAME DATA
# ═══════════════════════════════════════════════════════════════════════════════

TEAMS = [
    "Izvršni bord direktora",
    "Pravni tim",
    "IT/CERT tim",
    "PR tim",
    "Policija/Tužilaštvo",
    "Diplomatija",
]

ROLE_CARDS = {
    "Izvršni bord direktora": {
        "icon": "🏛️",
        "role": "Donosite strateške odluke: kontinuitet snabdevanja, otkup, odgovornost prema građanima, reputacija i odnos sa državnim organima.",
        "secret": "Kompanija je prethodnih meseci bila predmet neformalnog interesovanja stranog investicionog fonda za privatizaciju dela poslovanja.",
        "dilemma": "Da li platiti otkup ako blackout traje i kako saopštiti javnosti da su podaci možda procureli?"
    },
    "Pravni tim": {
        "icon": "⚖️",
        "role": "Vodite zakonitost postupanja, prijavu incidenta, odnos prema Povereniku, dokaze i komunikaciju sa tužilaštvom.",
        "secret": "Postoje indicije da su eksfiltrirani podaci građana: imena, adrese, brojevi mernih mesta, iznosi računa i dugovanja.",
        "dilemma": "Koliko brzo i koliko detaljno obavestiti javnost, Poverenika i nadležne organe, a da se ne ugrozi istraga?"
    },
    "IT/CERT tim": {
        "icon": "💻",
        "role": "Stabilizujete elektrodistributivni sistem, izolujete kompromitovane segmente i čuvate forenzičke tragove.",
        "secret": "Prvi trag ukazuje na kompromitovan VPN nalog dobavljača. Backup postoji, ali nije testiran šest meseci.",
        "dilemma": "Da li brzo izolovati segmente i produžiti prekid ili održati deo sistema online i rizikovati širenje ransomware-a?"
    },
    "PR tim": {
        "icon": "📣",
        "role": "Vodite kriznu komunikaciju prema medijima, građanima i zaposlenima, uz cilj sprečavanja panike.",
        "secret": "Jedan veliki portal najavljuje tekst za 10 minuta: 'Hakeri ugasili struju i ukrali podatke građana'.",
        "dilemma": "Da li izaći sa delimičnim informacijama odmah ili sačekati potvrđene tehničke i pravne podatke?"
    },
    "Policija/Tužilaštvo": {
        "icon": "🔍",
        "role": "Vodite operativno-pravni deo: digitalni tragovi, kvalifikacija dela, dokazne radnje, međunarodna saradnja i lanac dokaza.",
        "secret": "Stil ransom poruke i naziv leak kanala liče na grupu koja je ranije napadala energetske subjekte.",
        "dilemma": "Da li prioritet dati brzom prikupljanju podataka od stranih provajdera ili tihoj istrazi bez javnog uzbunjivanja?"
    },
    "Diplomatija": {
        "icon": "🌐",
        "role": "Procena međunarodnopravnog i diplomatskog odgovora, posebno u vezi sa atribucijom, stranim interesima i mogućom eskalacijom.",
        "secret": "Kineski investicioni fond je prethodnih meseci pokazivao interesovanje za privatizaciju dela EDG.",
        "dilemma": "Kako reagovati ako tehnički tragovi vode ka grupi povezanoj sa kineskim interesima, ali bez dokaza za državnu atribuciju?"
    }
}

PHASES = [
    {
        "title": "FAZA 1 — Blackout i ransomware nad OT/SCADA sistemom",
        "clock": "08:45",
        "narrative": "EDG, operater elektrodistributivnog sistema i deo kritične infrastrukture, prijavljuje ozbiljan poremećaj. Dispečerski centar gubi kontrolu nad delom sistema za distribuciju električne energije. Jedan deo grada ostaje bez struje. Na OT/SCADA radnim stanicama pojavljuje se ransomware poruka.",
        "visual": "CRITICAL SYSTEM FAILURE\n\nYour SCADA systems are encrypted.\nPower distribution is disrupted.\nWe have exfiltrated customer personal data,\nbilling information and operational documents.\n\nTime remaining: 72:00:00",
        "inject": "Građani prijavljuju da semafori ne rade. Mediji pitaju da li je kvar ili sajber napad.",
        "questions": {
            "shared": ["Koji je prvi prioritet kriznog štaba u trenutku potvrde sajber napada na kritičnu infrastrukturu?",
                ["Odmah obavestiti medije da se spreči panika",
                 "Stabilizovati distribuciju struje i sprečiti širenje incidenta",
                 "Sačekati kompletnu tehničku analizu pre bilo kakve akcije",
                 "Kontaktirati napadače radi pregovora"],
                1, "Kontinuitet usluge i zaustavljanje širenja uvek imaju prednost nad komunikacijom i pregovorima u prvim minutima krize."],
            "Izvršni bord direktora": [
                ["Ko ima ovlašćenje da proglasi krizno stanje i predsedava kriznim štabom?",
                 ["Svaki rukovodilac samostalno prema proceni",
                  "Generalni direktor ili ovlašćeno lice prema kriznom protokolu kompanije",
                  "IT menadžer kao jedini tehnički ekspert",
                  "Portparol prema medijima"],
                 1, "Krizni protokol mora jasno definisati liniju komandovanja — improvizacija u krizi povećava haos."],
                ["Da li EDG sme nastaviti isporuku struje kompromitovanim segmentima dok traje incident?",
                 ["Da, svaki prekid je veća šteta od bezbednosnog rizika",
                  "Ne, kompromitovani segmenti moraju biti izolovani čak i po cenu privremenog prekida",
                  "Da, napadači ionako već imaju pristup pa izolacija nema smisla",
                  "Odluku donosi samo regulatorno telo, ne uprava"],
                 1, "Izolacija kompromitovanih segmenata sprečava lateralno kretanje napada i dodatne štete."],
                ["Koji organ vlasti EDG mora odmah obavestiti o incidentu koji ugrožava kritičnu infrastrukturu?",
                 ["Samo internu reviziju kompanije",
                  "Nadležni CERT, organ za bezbednost informacija i po potrebi policiju",
                  "Nikoga dok se ne utvrdi potpuna razmera incidenta",
                  "Samo akcionare i investitore"],
                 1, "Zakon o informacionoj bezbednosti propisuje obavezno obaveštavanje nadležnih u slučaju incidenta na sistemu od posebnog značaja."]
            ],
            "Pravni tim": [
                ["Pod kojim zakonima se ransomware napad na operatora kritične infrastrukture pravno kvalifikuje?",
                 ["Zakon o obligacionim odnosima i Zakon o zaštiti potrošača",
                  "Zakon o informacionoj bezbednosti i relevantne odredbe Krivičnog zakonika",
                  "Zakon o javnim nabavkama i interni akti kompanije",
                  "Nije posebno zakonski regulisano — primenjuje se opšti obligacioni okvir"],
                 1, "ZIB i KZ predviđaju poseban tretman napada na sisteme od posebnog značaja, uključujući kritičnu infrastrukturu."],
                ["Ko ima zakonsku obavezu da prijavi incident bezbednosti IKT sistema od posebnog značaja?",
                 ["Bilo koji zaposleni koji prvi sazna za incident",
                  "Operator sistema (EDG) nadležnom CERT-u u zakonski propisanom roku",
                  "Samo IT menadžer putem internog izveštaja",
                  "Obaveza prijave nastaje tek nakon utvrđivanja finansijske štete"],
                 1, "ZIB propisuje obavezu operatora da prijavi incident CERT-u u određenom roku — kasna prijava je sama po sebi prekršaj."],
                ["Da li EDG ima pravo da bez sudskog naloga sačuva sve mrežne logove kao dokaz u slučaju sajber napada?",
                 ["Ne, logovi sadrže lične podatke i ne mogu se čuvati bez naloga",
                  "Da, u slučaju sajber incidenta postoji zakonski osnov za hitno obezbeđenje elektronskih dokaza",
                  "Samo uz pisanu saglasnost svih zaposlenih koji su koristili sistem",
                  "Logovi se automatski brišu i nema zakonske obaveze njihovog čuvanja"],
                 1, "Hitno obezbeđenje dokaza pre nego što se unište je zakonska obaveza i conditio sine qua non uspešne istrage."]
            ],
            "IT/CERT tim": [
                ["Koji je tehnički prioritet u prvim minutima nakon detekcije ransomware-a na OT/SCADA sistemu?",
                 ["Pokrenuti antivirusno skeniranje svih sistema odmah",
                  "Izolovati zaražene segmente od ostatka mreže uz istovremeno očuvanje forenzičkih tragova",
                  "Odmah ugasiti sve servere i radne stanice",
                  "Restartovati sve sisteme i videti da li se problem sam rešava"],
                 1, "Izolacija zaustavlja lateralno kretanje, a očuvanje forenzičkih tragova je preduslov za kasniju istragu i oporavak."],
                ["Koji od sledećih postupaka je POGREŠAN u prvoj fazi odgovora na ransomware incident?",
                 ["Isključivanje kompromitovanih segmenata iz mreže",
                  "Pravljenje forenzičke kopije memorije zaraženih sistema",
                  "Brisanje ransomware poruke i čišćenje sistema pre forenzičke analize",
                  "Kontaktiranje nadležnog CERT-a i obaveštavanje rukovodstva"],
                 2, "Brisanje pre forenzike uništava ključne tragove — identifikacija vektora napada i ključeva za dekripciju postaje nemoguća."],
                ["Kompromitovan VPN nalog dobavljača je verovatni vektor napada. Šta odmah preduzimate?",
                 ["Resetovati samo lozinku za taj nalog i nastaviti normalan rad",
                  "Deaktivirati nalog, pregledati sve sesije i logove tog naloga, pokrenuti forenziku i obavestiti dobavljača",
                  "Obrisati nalog bez forenzičke analize da se spreče dalje povrede bezbednosti",
                  "Sačekati da dobavljač sam prijavi problem i preduzme mere"],
                 1, "Kompromitovan vendor nalog zahteva hitnu deaktivaciju i sveobuhvatnu forenziku — napadač možda i dalje ima pristup."]
            ],
            "PR tim": [
                ["Mediji pitaju 'Da li je ovo hakerski napad?' — šta je ispravna prva izjava kompanije?",
                 ["Da, napadnuti smo i situacija je kritična — transparentnost je najvažnija",
                  "Istražujemo poremećaj u radu sistema i daćemo ažuriranu informaciju čim proverimo činjenice",
                  "Nema napada — radi se o tehničkom kvaru sistema",
                  "Bez komentara do okončanja celokupne istrage"],
                 1, "Prva izjava mora biti tačna ali ne preuranjena — 'istražujemo' je istinito i ne stvara lažna očekivanja."],
                ["Šta mora biti usaglašeno pre objavljivanja prvog zvaničnog saopštenja?",
                 ["Samo PR tim odlučuje — komunikacija je isključivo njihov domen",
                  "Saopštenje mora biti usaglašeno sa pravnim timom i IT/CERT-om pre objavljivanja",
                  "Dovoljno je usmeno odobrenje generalnog direktora bez konsultacija",
                  "Saopštenje se ne daje dok istraga nije u potpunosti završena"],
                 1, "Nesinhronizovana komunikacija stvara kontradikcije koje mediji odmah iskoriste — usaglašavanje je obaveza."],
                ["Koji kanal je prioritetan za prvu komunikaciju prema građanima čije je snabdevanje prekinuto?",
                 ["Konferencija za štampu — jedino formalna komunikacija ima težinu",
                  "Zvanični veb sajt, društvene mreže i SMS obaveštenja korisnicima",
                  "Jedino televizija jer drugi kanali nisu dovoljno pouzdani",
                  "Sačekati da novinari sami objave informacije pa reagovati"],
                 1, "Višekanalni pristup dostiže maksimalan broj korisnika u najkraćem roku — kombinacija digitalnih i direktnih kanala je optimalna."]
            ],
            "Policija/Tužilaštvo": [
                ["Ransomware napad na operatora kritične infrastrukture — po kojim odredbama KZ se vrši kvalifikacija?",
                 ["Prevara (čl. 208 KZ) kao osnovna kvalifikacija",
                  "Neovlašćen pristup zaštićenom računaru uz otežavajuće okolnosti napada na kritičnu infrastrukturu (čl. 302 i 305 KZ)",
                  "Oštećenje tuđe stvari (čl. 212 KZ) jer su sistemi oštećeni",
                  "Iznuda (čl. 214 KZ) jedina je relevantna kvalifikacija zbog zahteva za otkupom"],
                 1, "Napad na sisteme kritične infrastrukture kvalifikuje se po posebnim odredbama koje predviđaju teže kazne od opšte prevare."],
                ["Koji je prvi korak policije u obezbeđenju digitalnih dokaza na licu mesta?",
                 ["Isključiti sve uređaje da se spreči dalje širenje ransomware-a",
                  "Napraviti forenzičke kopije (bit-by-bit) bez izmene originalnih medija uz dokumentovanje lanca čuvanja",
                  "Fotografisati ekrane zaraženih računara — to je dovoljno za sudski postupak",
                  "Najpre prikupiti iskaze zaposlenih pa tek onda tehničku forenziku"],
                 1, "Forenzička kopija bez izmene originala i lanac čuvanja (chain of custody) su preduslov prihvatljivosti digitalnih dokaza na sudu."],
                ["Ko u Republici Srbiji ima primarnu nadležnost za vođenje istrage sajber napada na kritičnu infrastrukturu?",
                 ["MUP, Odeljenje za borbu protiv visokotehnološkog kriminala, bez učešća tužilaštva",
                  "Tužilaštvo za visokotehnološki kriminal u koordinaciji sa BIA i MUP-om",
                  "Regulatorno telo za energetiku jer je napadnut energetski sektor",
                  "Svako tužilaštvo opšte nadležnosti prema mestu izvršenja"],
                 1, "Tužilaštvo za VTK ima specijalizaciju i nadležnost za ovakve predmete, uz obaveznu koordinaciju sa BIA i MUP-om."]
            ],
            "Diplomatija": [
                ["Da li Diplomatija treba da javno komentariše incident u prvim satima pre atribucije?",
                 ["Da, odmah treba objaviti da Srbija neće tolerisati napade iz inostranstva",
                  "Ne, diplomatski aparat prati razvoj i priprema se diskretno bez javnog istupanja bez koordinacije",
                  "Da, treba odmah optužiti poznate sajber pretnje po principu 'show of force'",
                  "Diplomatija nema ulogu u ovoj ranoj fazi incidenta"],
                 1, "Prerano istupanje bez dokaza narušava kredibilitet i može ugroziti istragu — diplomatija se priprema, ne eksponira."],
                ["Šta diplomatija priprema u prvim satima a da ne istupa javno?",
                 ["Ništa, jer pre atribucije nema diplomatskih aktivnosti koje treba preduzeti",
                  "Pregled diplomatskih kanala, analizu potencijalnih aktera i diskretni kontakt sa saveznicima radi razmene informacija",
                  "Ultimatum svim državama koje imaju istoriju sajber napada",
                  "Javno saopštenje za međunarodnu štampu o ozbiljnosti situacije"],
                 1, "Pripremni rad u pozadini — analiza aktera, konsultacije sa saveznicima — daje diplomatiji prednost u kasnijem reagovanju."],
                ["Da li sajber napad na kritičnu infrastrukturu automatski aktivira klauzule kolektivne odbrane poput člana 5 NATO-a?",
                 ["Da, svaki sajber napad na kritičnu infrastrukturu automatski aktivira čl. 5",
                  "Ne, aktivacija zavisi od atribucije, razmere i konsenzusa saveznika — sajber napadi su posebno kompleksni slučajevi",
                  "Da, ali samo ako je potvrđeno direktno državno sponzorstvo napada",
                  "Ne, čl. 5 se eksplicitno ne primenjuje na digitalni prostor ni u jednoj okolnosti"],
                 1, "Sajber napad može biti 'armed attack' po čl. 5 ali to zahteva konsenzus saveznika i jasnu atribuciju — nije automatsko."]
            ]
        }
    },
    {
        "title": "FAZA 2 — Phishing finansija i zahtev za otkup",
        "clock": "09:15",
        "narrative": "Finansijski sektor dobija email koji navodno dolazi iz kancelarije finansijskog direktora i traži hitnu uplatu radi oporavka sistema. Zaposleni su pod pritiskom i traže instrukcije.",
        "visual": "From: CFO Office <finance-support@edg-secure.example>\nSubject: URGENT — Recovery Payment Required\n\nCEO has authorized emergency recovery payment.\nAmount: EUR 2,400,000 in Bitcoin\nDeadline: 4 hours\n\nFailure to comply will result in prolonged blackout\nand public release of all customer data.\n\nAttachment: payment_instructions.pdf",
        "inject": "Finansije pitaju krizni štab da li da postupe po instrukciji iz emaila.",
        "questions": {
            "shared": ["Finansijski sektor prima urgentni email od navodnog CEO-a sa zahtevom za hitnu uplatu. Koji je obavezan prvi korak?",
                ["Uplatiti odmah da se ne produžava blackout i šteta po građane",
                 "Suspendovati instrukciju i verifikovati autentičnost isključivo kroz nezavisni komunikacioni kanal",
                 "Odgovoriti na email i od pošiljaoca tražiti pisanu potvrdu",
                 "Odmah obavestiti medije da je kompanija napadnuta i kroz email"],
                1, "Verifikacija kroz nezavisni kanal (telefonski poziv na poznat broj) jedini je pouzdan način provere autentičnosti finansijske instrukcije."],
            "Izvršni bord direktora": [
                ["CEO fraud je pokušan u ime generalnog direktora. Kako to utiče na korporativnu odgovornost uprave?",
                 ["Uprava nije odgovorna jer je prevara krivično delo trećeg lica",
                  "Uprava je dužna da ima procedure verifikacije koje bi sprečile ovakvu prevaru — propust je i korporativna odgovornost",
                  "Odgovoran je isključivo IT sektor koji nije zaštitio email komunikaciju",
                  "Odgovornost snosi jedino zaposleni koji je primio i nije prijavio sumnjivi email"],
                 1, "Uprava ima dužnost da uspostavi efikasne kontrole — nedostatak verifikacionih procedura je propust upravljanja."],
                ["Da li Izvršni bord sme da odobri plaćanje otkupa napadačima bez konsultacija?",
                 ["Da, brza uplata je efikasnija od dugih konsultacija",
                  "Ne, plaćanje zahteva konsultaciju sa pravnim timom, CERT-om i organima — može biti krivično ili sankcionisano",
                  "Da, ako je iznos manji od procenjene štete od produženog blackouta",
                  "Da, ali samo ako se plaćanje izvrši potpuno anonimno"],
                 1, "Plaćanje otkupa kriminalnim organizacijama može narušiti sankcioni režim i privući krivičnu odgovornost uprave."],
                ["Koji su ključni faktori koje Izvršni bord mora sagledati pre odluke o plaćanju?",
                 ["Samo iznos otkupa i trenutna raspoloživa gotovina kompanije",
                  "Pravni rizik, garancija oporavka, uticaj na reputaciju, preporuka CERT-a i organa bezbednosti i osiguravajuće kuće",
                  "Isključivo mišljenje PR tima o tome kako će javnost reagovati",
                  "Samo stav osiguravajuće kuće — oni pokrivaju štetu"],
                 1, "Multidimenzionalna odluka zahteva input svih relevantnih timova — unilateralna finansijska odluka u krizi je visokorizična."]
            ],
            "Pravni tim": [
                ["Da li plaćanje otkupa u kriptovalutama može biti krivično sankcionisano ili kvalifikovano kao finansiranje kriminala?",
                 ["Ne, to je legitimni poslovni trošak koji kompanija može slobodno platiti",
                  "Da, u zavisnosti od atribucije napadača plaćanje može narušiti sankcioni režim ili se smatrati finansiranjem kriminalne organizacije",
                  "Samo ako iznos prelazi 10.000 EUR u jednoj transakciji",
                  "Ne, jer je to privatni sporazum između EDG i napadača bez javnopravnih posledica"],
                 1, "Plaćanje sancionisanim entitetima je zabranjeno — pre plaćanja neophodna je provera liste sankcija i pravno mišljenje."],
                ["Email dolazi sa domena koji liči na interni ali nije identičan. Kakav je to pravni kvalifikator za napadača?",
                 ["Prevara kroz lažno predstavljanje, kombinovano sa mogućom računarskom sabotatažom kao delom šire operacije",
                  "Samo administrativni prekršaj — nije krivično delo bez dokazane materijalne štete",
                  "Nije krivično kvalifikovano jer domen nije identičan internom — sličnost nije prevara",
                  "Jedino civilna odgovornost registra domena koji je dozvolio sličan naziv"],
                 0, "Lažno predstavljanje kombinovano sa finansijskim zahtevom je klasična prevara; upotreba IT alata je otežavajuća okolnost."],
                ["Zaposleni je kliknuo na prilog iz phishing emaila. Koje su obaveze kompanije odmah nakon toga?",
                 ["Disciplinska mera prema zaposlenom je dovoljna i jedina neophodna reakcija",
                  "Forenzička analiza uređaja, provera kompromitacije sistema i obaveza prijavljivanja ako se potvrdi povreda podataka",
                  "Nema posebnih obaveza ako odmah vidljiva šteta nije nastupila",
                  "Samo interno upozorenje svim zaposlenima emailom o opasnosti od phishinga"],
                 1, "Klik na phishing prilog je potencijalna povreda podataka — forenzika i prijava su obaveze, ne opcije."]
            ],
            "IT/CERT tim": [
                ["Email header pokazuje da poruka nije prošla SPF i DKIM proveru. Šta to znači za analizu?",
                 ["Greška u email serveru primaoca koja nije indikator napada",
                  "Email verovatno nije poslat sa legitimnog servera pošiljaoca — visok indikator email spoofinga",
                  "Email je legitiman ali je kasno isporučen zbog serviserskih problema",
                  "SPF i DKIM su zastareli standardi i njihov rezultat nije relevantan za analizu"],
                 1, "Neuspeh SPF/DKIM validacije je pouzdan tehnički indikator da email nije poslat od deklarisanog domena."],
                ["Koji tehnički pristup koristite za sigurnu analizu sumnjivog email attachmenta?",
                 ["Otvoriti attachment na standardnom Windows poslovnom računaru i pratiti šta se dešava",
                  "Sandbox analiza u potpuno izolovanoj virtuelnoj mašini bez mrežne konekcije",
                  "Proslediti attachment antivirusu na skeniranje i sačekati rezultat",
                  "Otvoriti na Linux sistemu jer malware ne radi na Linuxu"],
                 1, "Sandbox u izolovanoj VM jedini je siguran način analize — antivirusni skeneri propuštaju zero-day exploite."],
                ["Otkriven je C2 (Command & Control) saobraćaj ka inostranom IP-u. Koji je kompletan odgovor?",
                 ["Blokirati IP adresu na firewall-u i nastaviti normalan rad sistema",
                  "Blokirati saobraćaj, sačuvati sve mrežne logove, pokrenuti forenziku zaraženih sistema i odmah obavestiti CERT",
                  "Sačekati da napadač sam prekine C2 komunikaciju pre preduzimanja akcije",
                  "Kontaktirati ISP koji opslužuje vlasnika te IP adrese direktno"],
                 1, "Blokiranje bez logovanja uništava ključne forenzičke tragove — redosled je: loguj, sačuvaj, onda blokiraj."]
            ],
            "PR tim": [
                ["Novinari saznaju da EDG razmatra plaćanje otkupa napadačima. Koji je ispravan PR odgovor?",
                 ["Potvrditi: plaćamo da zaštitimo građane — transparentnost je prioritet",
                  "Ne možemo komentarisati operativne odluke u toku — radimo na oporavku sistema",
                  "Kategorički negirati: nikada nećemo platiti — čak i ako to nije tačno",
                  "Potvrditi da nećemo platiti i objasniti tehničke alternative"],
                 1, "Komentarisanje opcija u toku pregovora ili donošenja odluke otkriva napadaču poziciju kompanije i slabi pregovaračku moć."],
                ["Zaposleni spontano objavljuju fotografije ransomware poruke na društvenim mrežama. Šta radi PR?",
                 ["Ništa — sloboda izražavanja zaposlenih je zaštićena i kompanija ne može intervenisati",
                  "Hitna interna komunikacija o zabrani deljenja internih informacija i šta zaposleni smeju da kažu",
                  "Tužiti zaposlene za odavanje poslovne tajne kao primer drugima",
                  "Potvrditi medijima sadržaj poruke na osnovu objava zaposlenih"],
                 1, "Zaposleni su u panici i dele informacije — kompanija mora brzo uspostaviti jasne smernice, ne disciplinovanje."],
                ["Koji ton i sadržaj treba da ima saopštenje EDG-a u ovoj fazi?",
                 ["Agresivan — javno optužiti napadače i pokazati snagu kompanije",
                  "Miran, transparentan o onome što je potvrđeno, bez spekulacija o onome što nije utvrđeno",
                  "Minimalistički — što manje informacija objavljeno, to manje štete za reputaciju",
                  "Dramatičan i uznemirujući — da javnost shvati ozbiljnost i pritisne vladu da reaguje"],
                 1, "Kredibilna komunikacija gradi poverenje — dramatizacija i minimizacija jednako narušavaju reputaciju."]
            ],
            "Policija/Tužilaštvo": [
                ["CEO fraud kroz email — da li se to istražuje odvojeno od ransomware napada ili kao deo iste operacije?",
                 ["Odvojeno kao poseban predmet jer koristi drugačiji vektor napada",
                  "Kao deo koordinisane kriminalne operacije — oba vektora istovremeno ukazuju na istog aktora",
                  "Kao civilni spor između kompanije i napadača bez krivičnog elementa",
                  "Nije u nadležnosti Tužilaštva za VTK jer nema kompjuterskog kriminala"],
                 1, "Istovremeni ransomware i CEO fraud su karakteristika koordinisane operacije — razdvajanje istrage gubi vezu između vektora."],
                ["Da li se BTC adresa iz ransom poruke može koristiti kao dokaz i kako?",
                 ["Ne, kriptovalute su anonimne i blockchain nije prihvatljiv kao sudski dokaz",
                  "Da, blockchain analiza BTC adrese može pratiti tokove i identifikovati klastere koji se pripisuju napadaču",
                  "Samo ako napadači sami potvrde vlasništvo nad adresom",
                  "Iznos traženog otkupa je relevantan ali sama BTC adresa nije dokazni materijal"],
                 1, "Blockchain je javna knjiga — specijalizovani alati za blockchain analizu (Chainalysis, Elliptic) mogu pratiti tokove čak i kroz mixing."],
                ["Phishing email je poslat sa servera u inostranstvu. Koji je mehanizam za dobijanje podataka o tom serveru?",
                 ["Direktan neformalni kontakt sa hosting kompanijom bez formalnog zahteva — brže i efikasnije",
                  "Međunarodna pravna pomoć (MLA) ili hitni zahtev za čuvanje podataka kroz Budimpeštansku konvenciju o sajber kriminalu",
                  "OSINT analiza je dovoljna — formalni zahtevi traju predugo i ne donose rezultate",
                  "Sajber napad iz inostranstva je van naše jurisdikcije i ne možemo delati"],
                 1, "Budimpeštanska konvencija omogućava hitne zahteve za čuvanje podataka (preservation) dok se čeka formalni MLA."]
            ],
            "Diplomatija": [
                ["Phishing email dolazi sa servera registrovanog u inostranstvu. Da li to implicira državnu umešanost?",
                 ["Da, server u inostranstvu direktno ukazuje na državni akter te države",
                  "Ne nužno, privatni akteri i kriminalne grupe rutinski koriste infrastrukturu u raznim jurisdikcijama za anonimizaciju",
                  "Da, posebno ako je server u državi poznatoj po sajber napadima",
                  "Ne postoji veza između geografske lokacije servera i atribucije napadača"],
                 1, "Napadači svesno koriste infrastrukturu u trećim državama upravo da bi otežali atribuciju — ovo je standardna praksa."],
                ["Saveznici nude obaveštajne podatke o sličnim napadima i napadačima. Kako se to prima?",
                 ["Direktno od ambasadora na bilateralnom neformalnom sastanku za brzinu",
                  "Kroz zvanične obaveštajne i diplomatske kanale uz formalni zapis o primljenim podacima i metodologiji",
                  "Neformalno putem ličnih kontakata da bi se izbegla birokratska procedura",
                  "Srbija po principu ne prima obaveštajne podatke od saveznika o sajber pretnjama"],
                 1, "Formalni kanali i dokumentovanje su neophodni — neformalne razmene ne mogu biti korišćene u pravnim procedurama."],
                ["Incident privlači pažnju međunarodnih medija i ambasada. Koji je primarni zadatak diplomatije?",
                 ["Publično optužiti strane aktere pre završetka istrage da bi se pokazala odlučnost",
                  "Koordinisati komunikaciju sa saveznicima, sprečiti pogrešnu atribuciju i pratiti diplomatske reakcije stranih vlada",
                  "Predložiti prekid diplomatskih odnosa sa svim sumnjivim državama",
                  "Diplomatija nema ulogu dok istraga nije formalno završena i atribucija potvrđena"],
                 1, "Upravljanje međunarodnim narativom sprečava diplomatske incidente bazirane na pogrešnim pretpostavkama."]
            ]
        }
    },
    {
        "title": "FAZA 3 — Eksfiltracija podataka i obaveze obaveštavanja",
        "clock": "09:45",
        "narrative": "Na Telegram kanalu GRID_LEAKS objavljen je uzorak podataka: ime, adresa, broj mernog mesta i iznos računa. Poverenik za informacije od javnog značaja traži hitne informacije.",
        "visual": "CHANNEL: @GRID_LEAKS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nBlackout is just the beginning.\n\nWe have customer records:\n→ Full names & addresses\n→ Meter IDs & billing history\n→ Payment status & debt records\n\nSample (100 records) attached.\nFull release in 6 hours unless payment.\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "inject": "Stiže dopis Poverenika: tražen opis incidenta, kategorije podataka, broj lica i mere ublažavanja.",
        "questions": {
            "shared": ["Koji je zakonski rok za prijavu povrede podataka o ličnosti Povereniku po Zakonu o zaštiti podataka o ličnosti?",
                ["30 dana od otkrivanja povrede podataka",
                 "72 sata od saznanja za povredu podataka, bez nepotrebnog odlaganja",
                 "Rok ne postoji — prijava se vrši po okončanju istrage",
                 "Rok je 24 sata ali samo za zdravstvene i finansijske podatke"],
                1, "ZZPL čl. 53 propisuje rok od 72 sata za prijavu Povereniku — kašnjenje je samo po sebi prekršaj."],
            "Izvršni bord direktora": [
                ["Koji nivo transparentnosti prema javnosti je ispravan kada su podaci objavljeni na Telegramu?",
                 ["Potpuna tišina — svaka informacija narušava reputaciju i korisna je samo napadačima",
                  "Kontrolisana transparentnost — potvrđene informacije uz savete građanima bez spekulacija",
                  "Objaviti odmah sve tehničke detalje da javnost vidi ozbiljnost problema",
                  "Prebaciti svu komunikaciju na državne organe i povući se iz javne sfere"],
                 1, "Kontrolisana transparentnost gradi poverenje — tišina se tumači kao prikrivanje, a prevelika otvorenost može ugroziti istragu."],
                ["Koji je finansijski i reputacioni rizik od potencijalnih tužbi pogođenih građana?",
                 ["Minimalan — kompanija nije kriva za napad i ne može biti odgovorna za dela trećih lica",
                  "Značajan — EDG može biti odgovoran za nedovoljnu zaštitu podataka bez obzira na napadačev čin",
                  "Rizik postoji jedino ako pogođeni građani podnesu kolektivnu tužbu",
                  "Rizik postoji samo ako su objavljeni podaci doveli do dokazive direktne finansijske štete"],
                 1, "Odgovornost rukovatelja podataka je objektivna — nedovoljna zaštita je osnov odgovornosti bez obzira na uzrok povrede."],
                ["EDG razmatra angažovanje eksternog kriznog PR tima i pravnih savetnika. Kada je pravi momenat?",
                 ["Posle okončanja istrage kada se zna puna slika situacije",
                  "Odmah — eksterna ekspertiza za krizne situacije treba biti angažovana što pre u toku incidenta",
                  "Samo ako interni tim ne može da izađe na kraj sa situacijom sam",
                  "Eksterna podrška nije potrebna — interni timovi uvek bolje razumeju situaciju"],
                 1, "Brzina reakcije u kriznim komunikacijama je kritična — čekanje na 'pravi momenat' znači propuštanje ključnih 72 sata."]
            ],
            "Pravni tim": [
                ["Koji podaci su obuhvaćeni obavezom prijave Povereniku po objavi uzorka na Telegramu?",
                 ["Samo finansijski podaci kao najosetljivija kategorija",
                  "Svi lični podaci čija je bezbednost narušena: ime, adresa, broj mernog mesta, iznos računa, status dugovanja",
                  "Samo podaci koji nisu prethodno bili javno dostupni na internetu",
                  "Prijava nije potrebna jer je podatke objavio napadač, a ne EDG"],
                 1, "ZZPL ne pravi razliku po tome ko je objavio podatke — svaka neovlašćena obrada je povreda koja zahteva prijavu."],
                ["Koje su minimalne informacije koje EDG mora dostaviti Povereniku u prijavi povrede?",
                 ["Samo naziv kompanije, datum incidenta i kontakt podatke",
                  "Opis incidenta, kategorije i procenjeni broj pogođenih lica, preduzete mere, kontakt DPO-a, potencijalne posledice",
                  "Interni IT izveštaj o tehničkim aspektima napada",
                  "Samo potvrdu da je incident u toku istrage i da će detalji uslediti"],
                 1, "ZZPL čl. 53 propisuje tačan sadržaj prijave — nepotpuna prijava je sama po sebi kršenje zakona."],
                ["Da li EDG mora direktno i individualno obavestiti pogođene građane o curenju njihovih podataka?",
                 ["Ne, dovoljno je obavestiti Poverenika — on obaveštava javnost",
                  "Da, ukoliko povreda može prouzrokovati visok rizik za prava i slobode, EDG mora direktno obavestiti pogođene",
                  "Samo one koji su se već žalili ili kontaktirali korisničku podršku",
                  "Obaveštavanje građana je isključivo ovlašćenje Poverenika, ne obaveza EDG-a"],
                 1, "ZZPL čl. 54 propisuje direktno obaveštavanje pogođenih lica kada postoji visok rizik — EDG ne može prenebreći ovu obavezu."]
            ],
            "IT/CERT tim": [
                ["Kako forenzički utvrditi da li su objavljeni podaci zaista iz EDG baza podataka?",
                 ["Verujemo napadačima na reč — zašto bi lažirali uzorak podataka",
                  "Verifikacija uzorka poređenjem sa internim bazama uz forenzičku analizu mogućih vektora eksfiltracije",
                  "Proglasiti sve lažnim do dokaza suprotnog — teret dokaza je na napadaču",
                  "To nije u nadležnosti IT tima — procenu rade pravnici"],
                 1, "Verifikacija autentičnosti je preduslov za pravovremenu prijavu — lažne alarme i realne incidente treba razlikovati forenzički."],
                ["Koji forenzički artefakti su ključni za utvrđivanje vektora eksfiltracije podataka?",
                 ["Isključivo antivirusni logovi koji beleže malware aktivnost",
                  "Mrežni flow logovi, DLP upozorenja, pristupni logovi baze podataka, endpoint logovi i proxy logovi",
                  "Email arhiva kompanijskog servera kao jedino relevantno mesto",
                  "Fizički pregled serverske sobe i kontrola pristupa"],
                 1, "Eksfiltracija ostavlja tragove na više nivoa — korelacija različitih izvora logova jedina daje kompletnu sliku."],
                ["Backup nije testiran 6 meseci. Koji je ispravan postupak pre pokušaja oporavka sistema?",
                 ["Odmah koristiti backup bez testiranja — svaki minut prekida korisnicima košta",
                  "Testirati integritet i konzistentnost backup-a u izolovanom okruženju pre primene na produkciju",
                  "Odbaciti neispitan backup i izgraditi sistem od nule koristeći nove licence",
                  "Kontaktirati napadače za ključ za dekripciju jer je to brže od netestiranog backup-a"],
                 1, "Neispitan backup može biti zaražen ili konzistentno oštećen — primena na produkciju bez testa može pogoršati situaciju."]
            ],
            "PR tim": [
                ["Mediji objavljuju '1,2 miliona građana hakovano' — EDG još nije potvrdio tačan broj. Šta radi PR?",
                 ["Odmah potvrditi broj da se ne izgubi kredibilitet i medijima se čini usluga",
                  "Korigovati sa: 'Istraga utvrđuje tačan obim — nije potvrđeno da su svi podaci kompromirani'",
                  "Ignorisati medijsku objavu i fokusirati se na tehničke aspekte",
                  "Tužiti medij za objavljivanje neproverenih informacija"],
                 1, "Potvrđivanje nepotvrđenih brojeva je greška — korekcija sa pozivom na tačnost gradi kredibilitet."],
                ["Koje savete EDG treba da da građanima čiji su podaci potencijalno kompromitovani?",
                 ["'Sve je pod kontrolom — nema razloga za brigu i posebne akcije'",
                  "Promeniti lozinke, biti oprezan prema phishing pokušajima, pratiti sumnjive transakcije, kontaktirati banku o anomalijama",
                  "'Ne možemo davati savete dok istraga nije završena'",
                  "'Vaši podaci su sigurni' — čak i ako to nije potvrđeno forenzikom"],
                 1, "Konkretni i primenjivi saveti smanjuju štetu za građane i pozicioniraju EDG kao odgovornog aktera koji štiti korisnike."],
                ["Aktivisti za zaštitu privatnosti organizuju protest ispred sedišta EDG-a. Koji je ispravan odgovor?",
                 ["Ignorisati protest — svaka reakcija daje mu više medijskog prostora",
                  "Potvrditi razumevanje zabrinutosti, komunicirati preduzete mere i pozvati na konstruktivni dijalog",
                  "Pripremiti PR pobedničku izjavu koja minimizira incident i objašnjava zašto je protest neopravdan",
                  "Najaviti tužbe zbog narušavanja ugleda kompanije i ometanja rada"],
                 1, "Empatija i otvorenost za dijalog transformišu protestnu energiju u konstruktivni pritisak koji kompanija može koristiti."]
            ],
            "Policija/Tužilaštvo": [
                ["Telegram kanal objavljuje ukradene podatke. Koji je pravni mehanizam prema Telegram platformi?",
                 ["Policija nema nikakvu nadležnost nad inostranom digitalnom platformom",
                  "Zahtev za hitno čuvanje podataka (preservation request) i pravna pomoć kroz Budimpeštansku konvenciju",
                  "Direktan zahtev Telegramovim administratorima bez pravnog osnova — brže i efikasnije",
                  "Jedino dokumentovanje objave — nema pravnih mogućnosti akcije prema inostranoj platformi"],
                 1, "Budimpeštanska konvencija ima specifičan mehanizam za hitne zahteve prema platformama u potpisnicama — EU i SAD su potpisnice."],
                ["Koji je prioritet — brzo uklanjanje Telegram objave ili forenzička dokumentacija?",
                 ["Brzo uklanjanje je prioritet jer sprečava dalje širenje i sekundarnu štetu",
                  "Najpre forenzička dokumentacija (screenshot, URL, metadata, arhiviranje), pa koordinisano uklanjanje",
                  "Oba su jednaki prioriteti — raditi paralelno bez razlikovanja redosleda",
                  "Forenzika Telegram objave nije pravno relevantna jer je javno dostupna svima"],
                 1, "Uklanjanje bez dokumentacije uništava dokaz — redosled je fiksan: najpre sačuvati, pa ukloniti."],
                ["Blockchain analiza pokazuje da ransom BTC adresa koristi mixing servis. Šta to znači za istragu?",
                 ["Istraga je praktično završena — mixing servisi su kriptografski neprobojna prepreka",
                  "Otežava praćenje ali specijalizovani blockchain analitički alati i međunarodna saradnja mogu pratiti tokove i dalje",
                  "Nedvosmisleno ukazuje da su napadači iz Rusije zbog poznatih mixing servisa",
                  "Mixing ukazuje na neiskusne napadače koji se boje praćenja — to je pozitivan signal za istragu"],
                 1, "Chainalysis i slični alati uspešno deobfuskuju mixing servise u kombinaciji sa razmenom informacija između agencija."]
            ],
            "Diplomatija": [
                ["Ukradeni podaci uključuju informacije građana koji su i EU državljani. Koja međunarodna pravila mogu biti relevantna?",
                 ["Nikakva, GDPR se ne primenjuje u Republici Srbiji ni pod kojim uslovima",
                  "EDG primenjuje ZZPL, ali ako obrađuje EU podatke može imati obaveze prema GDPR-u; EU partneri imaju legitiman interes",
                  "Jedino UN Konvencija o sajber kriminalu koja je globalno obavezujuća",
                  "Međunarodni propisi se ne primenjuju na nacionalne energetske kompanije ni u jednom scenariju"],
                 1, "Ekstrateritorijalna primena GDPR-a je relevantna pitanje — diplomatija treba da bude svesna EU interesa u ovom predmetu."],
                ["Leak aktivira pažnju ENISA-e i EU partnera koji nude saradnju. Kako diplomatija odgovara?",
                 ["Ljubazno odbiti saradnju jer je to unutrašnja stvar Srbije i nema osnova za strani input",
                  "Prihvatiti saradnju i razmeniti relevantne informacije u skladu sa bilateralnim sporazumima i procedurama",
                  "Čekati da EU agencije same donesu zaključke i onda reagovati na njihove nalaze",
                  "Diplomatija ne komunicira sa tehničkim agencijama poput ENISA-e — to je domen IT timova"],
                 1, "ENISA ima iskustvo sa sličnim incidentima u EU — saradnja donosi threat intelligence i podršku bez gubitka suvereniteta."],
                ["Podaci srpskih građana cirkulišu na inostranim darkweb forumima. Koji je diplomatski instrument?",
                 ["Javna osuda vlada čiji su državljani preuzeli ili koristili te podatke",
                  "Koordinacija sa partnerima za identifikaciju i uklanjanje korišćenjem bilateralnih sporazuma i MLAT mehanizama",
                  "Srbija nema instrumente za delovanje van sopstvene teritorije u digitalnom prostoru",
                  "Ne postoji diplomatski instrument za ovakve situacije — to je isključivo pitanje sajber bezbednosti"],
                 1, "MLAT i bilateralni sporazumi o policijskoj saradnji su konkretni instrumenti koji se koriste u ovim situacijama."]
            ]
        }
    },
    {
        "title": "FAZA 4 — AI deepfake voice i sekundarni WhatsApp nalog",
        "clock": "10:10",
        "narrative": "CFO dobija WhatsApp glasovnu poruku sa nepoznatog broja, ali pod imenom CEO-a. Glas je ubedljiv i nalaže hitno plaćanje otkupa. Član uprave pritiska CFO-a da postupa po naredbi.",
        "visual": "📱 WhatsApp Voice Message\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nSender: CEO [UNKNOWN NUMBER]\nDuration: 0:47\nReceived: 10:09 AM\n\nTranscript:\n'Listen carefully. I am in a meeting and\ncannot use my regular phone. The lawyers\nhave approved the payment. Authorize it\nnow — we cannot afford more delay.\nThis is a direct instruction. I will\nexplain everything later.'\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ Sent from unregistered number",
        "inject": "Član uprave pritiska CFO-a: 'Ako je stvarno direktor, ne smemo gubiti vreme na procedure.'",
        "questions": {
            "shared": ["Glasovna poruka sa nepoznatog broja zvuči kao CEO i zahteva hitnu uplatu. Koji je ključni indikator da ovo može biti prevara?",
                ["Visina iznosa — legitimne finansijske naredbe uvek dolaze u pisanoj formi",
                 "Nepoznat broj, neuobičajeni kanal za finansijske naredbe, pritisak i hitnost bez mogućnosti verifikacije",
                 "CEO nikad ne komunicira glasovnim porukama u hitnim situacijama",
                 "Nema indikatora — glas zvuči legitimno i to je dovoljno za postupanje"],
                1, "Kombinacija nepoznatog broja, neuobičajenog kanala i izrazitog pritiska je klasičan obrazac CEO fraud napada."],
            "Izvršni bord direktora": [
                ["Član uprave pritiska CFO-a da plati bez verifikacije. Kako Izvršni bord mora reagovati?",
                 ["Dozvoliti CFO-u da sam proceni situaciju i donese odluku prema sopstvenoj proceni",
                  "Jasno potvrditi da bez verifikacije kroz poznate krizne kanale nema finansijskih transakcija — pritisak ne menja protokol",
                  "Sazvati hitnu sednicu da se glasanjem odluči o isplati",
                  "Kontaktirati generalnog direktora tek posle eventualne transakcije da se retroaktivno odobri"],
                 1, "Procedure verifikacije postoje upravo za ovakve situacije — pritisak autoriteta je manipulativna tehnika koja protokol ne sme zaobići."],
                ["Pokazalo se da je glasovna poruka bila AI deepfake. Koje procedure uprava mora hitno uvesti?",
                 ["Zabraniti korišćenje WhatsApp-a i svih privatnih platformi za poslovnu komunikaciju",
                  "Multi-channel verifikacioni protokol za finansijske transakcije i obuka zaposlenih o prepoznavanju AI manipulacija",
                  "Promeniti generalnog direktora jer incident narušava poverenje javnosti i zaposlenih",
                  "AI deepfake je previše nova tehnologija — efikasne procedure nije moguće kreirati odmah"],
                 1, "Multi-channel verifikacija (poziv na zvanični broj, fizička potvrda) je najefikasnija i odmah implementabilna zaštita."],
                ["Koji je reputacioni rizik i kako ga Izvršni bord adresira prema javnosti?",
                 ["Minimalan jer kompanija nije kriva — napad trećeg lica ne nanosi trajnu reputacionu štetu",
                  "Značajan — objaviti da je prevara prepoznata i sprečena, i komunicirati uvedene mere zaštite",
                  "Sakriti incident jer javnost ne treba da zna da su ovakvi napadi uopšte mogući",
                  "Dati otkaz odgovornom zaposlenom javno kao dokaz da kompanija ozbiljno shvata bezbednost"],
                 1, "Proaktivna komunikacija o sprečenoj prevari gradi poverenje — tajenje povećava štetu kada se otkrije."]
            ],
            "Pravni tim": [
                ["Korišćenje AI-generisanog glasa u svrhu prevare — kako se kvalifikuje po važećem KZ Srbije?",
                 ["Nije krivično delo jer AI sistem, a ne čovek, generiše glas koji se koristi",
                  "Prevara uz upotrebu informacione tehnologije, moguće kombinovana sa računarskom sabotatažom kao deo šire kriminalne operacije",
                  "Jedino krivična odgovornost kompanije koja je razvila AI sistem korišćen za prevaru",
                  "Isključivo parnična odgovornost — krivičnog elementa nema jer nije primenjena direktna sila"],
                 1, "KZ čl. 208 i čl. 302 pokrivaju ovakvu situaciju — AI je sredstvo izvršenja, a odgovornost je na licu koje ga koristi."],
                ["Da li bi EDG bio odgovoran za finansijski gubitak da je CFO platio bez verifikacije?",
                 ["Ne, gubitak uzrokovan krivičnim delom trećeg lica isključuje odgovornost kompanije",
                  "Delimično — kompanija ima dužnost da ima procedure koje sprečavaju ovakvu prevaru; propust je osnov odgovornosti",
                  "Odgovornost bi snosio isključivo CFO lično jer je on doneo odluku o plaćanju",
                  "Odgovornost je isključivo na WhatsApp platformi koja nije zaštitila kanal komunikacije"],
                 1, "Propust u uspostavljanju adekvatnih kontrolnih procedura može biti osnov odgovornosti kompanije pored krivice napadača."],
                ["Da li EDG može tužiti kompaniju čiji AI glas sinteze je korišćen za deepfake?",
                 ["Da, automatski jer su kreirali tehnologiju koja je zloupotrebljena",
                  "Izuzetno teško — AI alati su opšte namene; tužba zahteva dokaz da je tehnologija svesno napravljena za prevaru",
                  "Da, lako — postoji jasna uzročna veza između AI alata i nastale štete",
                  "Ne, AI kompanije imaju zakonski imunitet za zloupotrebu svojih proizvoda od strane trećih lica"],
                 1, "Odgovornost proizvođača AI alata je kontroverzna i teška za dokazivanje — primarna odgovornost ostaje na napadaču."]
            ],
            "IT/CERT tim": [
                ["Koji tehnički alati i metode postoje za detekciju AI-generisanog govora?",
                 ["Ne postoje pouzdani alati jer AI glas je danas praktično nedetektabilan",
                  "Deepfake detection alati, spektralna analiza audio karakteristika i metadata analiza audio fajla",
                  "Jedino dobro uvežbano ljudsko uho može pouzdano razlikovati AI glas od pravog",
                  "Analiza je tehnički moguća ali jedino u specijalizovanim laboratorijskim uslovima"],
                 1, "AI detekcija se brzo razvija — trenutni alati imaju ograničenja ali daju korisne indikatore uz spektralnu analizu."],
                ["WhatsApp glasovna poruka — koji su forenzički koraci za kompletnu analizu?",
                 ["Prepisati transkript i to je sasvim dovoljno za istragu",
                  "Sačuvati originalan audio fajl, ekstraktovati metadata, pokrenuti spektralnu i AI detekciju analizu, sačuvati hash vrednost",
                  "Proslediti audio fajl WhatsApp timu za bezbednost i čekati njihov odgovor",
                  "Forenzička analiza audio fajlova nije standardna kompetencija IT/CERT timova"],
                 1, "Celovita forenzika audio dokaza je ista kao i za bilo koji digitalni dokaz — originalni fajl, integritet i analiza metapodataka."],
                ["Nepoznati broj komunicira i sa drugim zaposlenima EDG-a. Šta je sveobuhvatan odgovor?",
                 ["Blokirati broj isključivo za CFO koji je primio poruku",
                  "Identifikovati sve zaposlene koji su primili poruke, prikupiti evidenciju, blokirati na svim uređajima i obavestiti sve",
                  "Sačekati da napadač ponovi pokušaj kako bi se prikupilo više forenzičkih podataka",
                  "Promeniti sve poslovne telefonske brojeve zaposlenih preventivno"],
                 1, "Sistemski odgovor koji pokriva sve exponirane zaposlene jedini sprečava uspeh napada kroz alternativni vektor."]
            ],
            "PR tim": [
                ["Mediji saznaju da je CFO umalo uplatio milione zbog glasovne poruke. Koji je PR odgovor?",
                 ["Potvrditi incident u potpunosti i detaljno opisati šta se desilo i ko je bio meta",
                  "'Naši bezbednosni protokoli su funkcionisali — pokušaj prevare je prepoznat i sprečen; istraga je u toku'",
                  "Kategorički negirati ceo incident jer je šteta od informacije veća od štete od samog pokušaja",
                  "Bez komentara — svaka informacija o ovom tipu napada škodi kompaniji"],
                 1, "Poruka da je prevara SPREČENA je pozitivna — fokus na funkcionisanju sistema gradi poverenje umesto da narušava."],
                ["Incident koriste konkurenti u PR kampanjama: 'EDG nije sigurna kompanija'. Kako PR odgovara?",
                 ["Direktan kontra-napad na konkurente kroz medije i društvene mreže",
                  "Fokusirati se na činjenice: incident je prepoznat, protokoli su funkcionisali, ulaganja u bezbednost se nastavljaju",
                  "Ignorisati jer svaka reakcija daje više vidljivosti konkurentskim tvrdnjama",
                  "Podneti tužbu za klevetu i poslovnu difamaciju"],
                 1, "Odgovor baziran na činjenicama i konkretnim merama delotvorniji je od emocionalnih ili agresivnih reakcija."],
                ["Zaposleni su uznemireni i pitaju mogu li i oni biti mete ovakvih napada. Kako PR interno komunicira?",
                 ["To je isključivo pitanje za HR — PR se bavi eksternom, ne internom komunikacijom",
                  "Jasna interna komunikacija: šta se desilo, kako prepoznati pokušaje, kome prijaviti i šta ne raditi",
                  "Slati obaveštenje samo menadžerima koji će sami proceniti šta prenose timovima",
                  "Čekati sa internom komunikacijom dok istraga nije formalno završena"],
                 1, "Uznemireni zaposleni koji nemaju informacije šire glasine — brza interna komunikacija smanjuje paniku i povećava oprez."]
            ],
            "Policija/Tužilaštvo": [
                ["Audio analiza potvrđuje da je glasovna poruka AI-generisana. Kako to menja krivičnu kvalifikaciju?",
                 ["Ne menja ništa — prevara ostaje prevara bez obzira na upotrebljenu tehnologiju",
                  "Upotreba AI može biti otežavajuća okolnost i može dodati elemente vezane za unapred planiranu kriminalnu operaciju",
                  "Smanjuje krivičnu odgovornost jer je AI delovao, a ne čovek direktno",
                  "AI prevare nisu pokrivene važećim KZ Srbije i ne mogu se krivično goniti"],
                 1, "Alat ne menja suštinu krivičnog dela — ali sofisticiranost upotrebe AI može uticati na odmeru kazne i kvalifikaciju."],
                ["Kako forenzički dokazati da je glasovna poruka AI-generisana, a ne originalan glas CEO-a?",
                 ["Dovoljno je pitati CEO-a da li je on snimio i poslao tu poruku",
                  "Ekspertska analiza audio spektra, poređenje sa autentičnim snimcima glasa, AI detekcija alati, metadata analiza fajla",
                  "Dovoljno je utvrditi da je poruka stigla sa neregistrovanog broja — to je dovoljan dokaz",
                  "Nije moguće pravno dokazati — AI glas je pravno neraspoznatljiv od pravog i sudovi to prihvataju"],
                 1, "Audio forenzika kombinovana sa AI detekcijom alata može biti veštačko dokazno sredstvo prihvatljivo na sudu."],
                ["CEO fraud je međunarodno rasprostranjen oblik kriminala. Koji mehanizmi međunarodne koordinacije postoje?",
                 ["Nema mehanizama — svaka istraga CEO frauda je strogo nacionalna bez međunarodne dimenzije",
                  "Interpol, Europol i EC3 imaju specijalizovane timove i mehanizme za CEO fraud i BEC istrage",
                  "Koordinacija je moguća samo ako je iznos prevare veći od praga od 10 miliona EUR",
                  "Međunarodna koordinacija je dostupna samo za EU članice — Srbija nije u poziciji da koristi te mehanizme"],
                 1, "Srbija ima bilateralne sporazume i pristup Interpol mehanizmima — EC3 Europol-a ima dedikovan program za BEC i CEO fraud."]
            ],
            "Diplomatija": [
                ["AI deepfake tehnologija korišćena u napadu na kompaniju — da li to ima implikacije za međunarodno pravo?",
                 ["Ne, AI je isključivo civilna tehnologija i nije pokrivena nijednim međunarodnim pravnim instrumentom",
                  "Da, pitanje odgovornosti za upotrebu AI u kriminalnim operacijama se razvija u međunarodnim forumima",
                  "Jedino ako je AI sistem razvijen u okviru zvaničnog državnog programa sajber operacija",
                  "Implikacije postoje jedino u vojnom kontekstu — civilne prevare nisu deo međunarodnog prava"],
                 1, "UN GGE i drugi forumi aktivno razmatraju pitanje AI u sajber operacijama — ovaj incident je relevantan case study."],
                ["Kako diplomatija može koristiti ovaj incident za jačanje međunarodnih normi o AI zloupotrebi?",
                 ["Javno optužiti vlade za čije se grupe pretpostavlja da razvijaju deepfake alate za ofanzivnu upotrebu",
                  "Koristiti incident kao case study u multilateralnim forumima (UN, OSCE) za jačanje normi odgovornog ponašanja",
                  "Pitanje AI normi nije diplomatski prioritet u ovom trenutku krize",
                  "Srbija je premala zemlja da bi mogla da utiče na međunarodne norme o AI"],
                 1, "Konkretni incidenti su najsnažniji argumenti u normativnim raspravama — Srbija može doprineti kroz vlastito iskustvo."],
                ["Strani novinari pitaju da li Srbija optužuje konkretnu stranu vladu za deepfake napad. Odgovor?",
                 ["Potvrditi sumnju na konkretnu državu jer to demonstrira ozbiljnost i odlučnost Srbije",
                  "'Istraga je u toku — atribucija zahteva čvrste tehničke dokaze; u ovom trenutku nema osnova za javnu optužbu'",
                  "Odbiti svako novinarsko pitanje koje se tiče napada jer su sva pitanja neprihvatljiva",
                  "'Da, sumnjamo, ali ne možemo to javno potvrditi' — implicitna optužba bez dokaza"],
                 1, "Javna optužba bez dokaza je diplomatski incident koji se teško povlači — strpljenje i čvrstina su jedina ispravna pozicija."]
            ]
        }
    },
    {
        "title": "FAZA 5 — OSINT, međunarodna saradnja i atribucija",
        "clock": "10:35",
        "narrative": "Tehnički indikatori vode ka stranom cloud provajderu. Domen je registrovan kroz anonimnog posrednika. OSINT analiza otkriva narativ na ruskim forumima da je 'privatizacija jedini izlaz' za EDG.",
        "visual": "OSINT SUMMARY REPORT\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nDomain: grid-recovery-support.example\nRegistrar: privacy-protected (Panama)\nHosting: Foreign cloud provider (AS routing TBD)\nC2 IP range: Eastern European exit node\n\nNarrative clusters detected:\n→ RU forums: 'privatization is the only solution'\n→ EN Telegram: 'EDG was always vulnerable'\n\nTTP overlap: Medium confidence\nAttribution: PRELIMINARY — NOT CONFIRMED\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "inject": "Strani novinar pita: 'Da li vaša vlada smatra da iza napada stoji Kina ili Rusija?'",
        "questions": {
            "shared": ["Koja je suštinska razlika između tehničke indikacije i formalne atribucije sajber napada?",
                ["Nema razlike — tehnički trag uvek i direktno vodi do konkretnog napadača",
                 "Tehnički indikatori su početna hipoteza koja zahteva verifikaciju; atribucija nosi pravne posledice i zahteva viši standard dokaza",
                 "Atribucija je brža i jednostavnija od tehničke analize koja je spora",
                 "OSINT analiza sama po sebi je dovoljan osnov za formalnu atribuciju i javno istupanje"],
                1, "Mešanje indikacije i atribucije je najčešća greška — javna atribucija bez adekvatnih dokaza je diplomatski i pravni rizik."],
            "Izvršni bord direktora": [
                ["Tehnička analiza ukazuje na inostrane aktere. Da li EDG to treba samostalno da saopšti javnosti?",
                 ["Da odmah — javnost ima pravo da zna i EDG duguje transparentnost korisnicima",
                  "Ne bez koordinacije sa organima bezbednosti i diplomatijom — prerana atribucija može ugroziti istragu",
                  "Da, ali bez navođenja konkretne države — dovoljno je reći 'inostrani akteri'",
                  "EDG ne treba da se bavi atribucijom uopšte — isključivo je to državni posao"],
                 1, "EDG nije organ za atribuciju — nekordinisano saopštavanje ugrozilo bi istragu i moglo izazvati diplomatske komplikacije."],
                ["Izvršni bord razmatra angažovanje privatne obaveštajne firme za OSINT. Da li je to prikladno?",
                 ["Da, privatne firme su brže i kompetentnije od državnih organa u OSINT analizi",
                  "Moguće uz stroge uslove: koordinacija sa nadležnim organima, jasni ugovorni okviri i zabrana aktivnosti van zakona",
                  "Ne, OSINT je isključiva nadležnost državnih organa i privatni sektor nema pravo na to",
                  "Prikladno bez ograničenja — privatni sektor nije vezan zakonskim ograničenjima kao državni"],
                 1, "Privatni OSINT može biti vredan resurs ali mora biti koordinisan sa istragom — nekontrolisani može ugroziti dokaze."],
                ["Koji finansijski instrumenti su dostupni EDG-u za pokriće troškova incidenta?",
                 ["Jedino interni budžet koji je prethodno bio planiran za ovu namenu",
                  "Kibernetičko osiguranje ako polisa postoji, državni fondovi za kritičnu infrastrukturu i tužbe za naknadu štete",
                  "Komercijalni kredit od poslovne banke je jedina realna opcija",
                  "Troškove snosi isključivo država kao vlasnik i operator kritične infrastrukture"],
                 1, "Kombinacija osiguranja, državne podrške i pravnih mehanizama za naknadu štete daje višeslojan finansijski odgovor."]
            ],
            "Pravni tim": [
                ["Koji je zakonski okvir za zahtev od stranog cloud provajdera da sačuva podatke o napadaču?",
                 ["Direktan email stranom provajderu sa opisom situacije — brže od formalnih mehanizama",
                  "Budimpeštanska konvencija — čl. 29 (hitno čuvanje) i MLA procedura za naknadni pristup sačuvanim podacima",
                  "GDPR zahtev koji se primenjuje na sve provajdere bez obzira na lokaciju",
                  "Ne postoji pravni okvir za zahteve prema inostranim provajderima u digitalnom prostoru"],
                 1, "Budimpeštanska konvencija je ključni instrument — čl. 29 omogućava hitno čuvanje pre nego što istekne rok čuvanja logova."],
                ["OSINT pokazuje TTP overlap sa poznatom kriminalnom grupom. Da li to daje osnov za krivičnu prijavu?",
                 ["Da, TTP overlap jednoznačno identifikuje napadača i dovoljan je osnov za prijavu",
                  "Daje osnov za intenziviranje istrage ali ne za prijavu — TTP overlap je indikator, ne dokaz identiteta",
                  "Ne, krivična prijava zahteva fizičko prisustvo osumnjičenog na teritoriji Srbije",
                  "Pravni tim ne treba da procenjuje tehničke indikatore — to je domen IT timova"],
                 1, "TTP overlap sužava krug osumnjičenih ali nije identifikacija konkretnih lica — istraga mora utvrditi individualne aktere."],
                ["Koji je pravni status OSINT dokaza u krivičnom postupku u Srbiji?",
                 ["OSINT nije prihvatljiv kao dokaz u krivičnom postupku Srbije",
                  "OSINT može biti prihvatljiv kao deo dokaznog materijala ako je prikupljen zakonito uz dokumentovanu metodologiju",
                  "OSINT je jednako validan kao i dokaz prikupljen sudskim nalogom",
                  "OSINT je validan jedino ako ga prikuplja policija — privatni OSINT se ne prihvata"],
                 1, "Dokumentovana metodologija i zakonitost prikupljanja su ključni za prihvatljivost — sud procenjuje dokaznu vrednost."]
            ],
            "IT/CERT tim": [
                ["Koji OSINT alati su standardni za analizu C2 infrastrukture i atribuciju napada?",
                 ["Google pretraga i Wikipedia su sasvim dovoljni za OSINT analizu u hitnoj situaciji",
                  "Shodan, VirusTotal, Censys, WHOIS analiza, passive DNS, threat intelligence platforme (MISP, OpenCTI)",
                  "Jedino alati koje je odobrio i licencirao MUP za upotrebu u istragama",
                  "OSINT analizu ne rade IT timovi — isključivo je u domenu obaveštajnih agencija"],
                 1, "Kombinacija ovih alata daje sveobuhvatnu sliku infrastrukture napadača i potencijalne TTP veze sa poznatim grupama."],
                ["Koji međunarodni standard koristite za opis TTP-ova u izveštaju o incidentu?",
                 ["Vlastiti interni format koji je poznat internom timu i dovoljno detaljan",
                  "MITRE ATT&CK framework — globalno prihvaćen standard za opis taktika, tehnika i procedura napadača",
                  "CVE baza podataka za identifikaciju iskorišćenih ranjivosti",
                  "ISO 27001 standard koji propisuje format bezbednosnih izveštaja"],
                 1, "MITRE ATT&CK je standard koji omogućava razmenu informacija sa međunarodnim partnerima i poređenje sa poznatim grupama."],
                ["Domen korišćen u napadu je registrovan 3 dana pre napada. Šta to sugeriše za istragu?",
                 ["Slučajnost — domeni se stalno registruju i nema automatske veze sa napadom",
                  "Visok indikator da je napad bio planiran unapred; kratko vreme između registracije i upotrebe karakteriše ciljane napade",
                  "Domen je verovatno registrovan od strane žrtve za odbrambene svrhe ali pogrešno konfigurisan",
                  "Ništa relevantno — registracija domena nije u domenu forenzičke analize"],
                 1, "Kratka 'životna dob' domena pre napada je jak indikator planiranog targeted napada, a ne oportunističkog."]
            ],
            "PR tim": [
                ["Strani novinari pitaju ko je napadač. PR tim nema informacije od istrage. Šta radi?",
                 ["Improvizovati odgovor na osnovu OSINT spekulacija koje su javno dostupne",
                  "'Naše nadležne institucije vode istragu — sve informacije o napadačima dolaze isključivo kroz zvanične kanale'",
                  "'Napadači su iz inostranstva' na osnovu OSINT analize bez navođenja konkretne države",
                  "'Bez komentara' na svako pitanje koje se tiče napada ili napadača"],
                 1, "Prebacivanje na zvanične kanale je jedina odgovorna opcija — svaka spekulacija PR tima bez osnova je opasna."],
                ["Mediji objavljuju vlastitu OSINT analizu i tvrde da znaju ko je napadač. Odgovor EDG PR-a?",
                 ["Potvrditi medijsku analizu ako vizuelno izgleda stručno i konzistentno",
                  "'Nismo u poziciji da komentarišemo spekulacije — pratite zvanične izjave nadležnih organa'",
                  "Negirati sve navode medija kako ne bi dobili prostor za dalje spekulacije",
                  "Pozvati novinare koji su radili analizu da podele metodologiju i podatke sa istragom"],
                 1, "Nepotvrditi, ne negirati, ne spekulisati — usmeriti na zvanične kanale je jedina bezbedna pozicija za PR."],
                ["Koji je uticaj OSINT spekulacija na javni pritisak na EDG? Kako PR to koristi konstruktivno?",
                 ["Ignorisati sve spekulacije jer je svaka reakcija kontraproduktivna",
                  "Koristiti javni interes kao argument za veća ulaganja u bezbednost i transparentniju komunikaciju o merama",
                  "Pokušati da kontrolišemo OSINT zajednicu i usmerimo je ka pogrešnim zaključcima",
                  "Zabraniti medijima da citiraju OSINT izvore jer nisu verifikovani"],
                 1, "Javni pritisak može biti transformisan u pozitivnu energiju — EDG koji transparentno komunicira o merama dobija poverenje."]
            ],
            "Policija/Tužilaštvo": [
                ["OSINT analiza identifikuje potencijalnog napadača u inostranstvu. Koji su mehanizmi identifikacije i hvatanja?",
                 ["Nema mehanizama — OSINT identifikacija nije dovoljna za bilo kakvu pravnu akciju",
                  "Interpol difuzija, crvena obaveštenja, MLA zahtev, Europol koordinacija i potencijalni zahtev za izručenje",
                  "Jedino direktna saradnja sa policijom te države bez formalnih kanala",
                  "Krivično gonjenje je moguće jedino ako osumnjičeni dobrovoljno dođe u Srbiju"],
                 1, "Kombinacija formalnih mehanizama — Interpol, MLA, Europol — daje najboljе izglede za identifikaciju i procesuiranje."],
                ["Koji su ključni forenzički dokazi potrebni za osnivanje optužnice?",
                 ["Samo OSINT dokazi i novinski izveštaji su dovoljni za podizanje optužnice",
                  "Digitalni forenzički dokazi sa chain of custody, veštačka mišljenja, finansijska dokumentacija, svedočenja i međunarodni dokazi",
                  "Dovoljno je da optuženi nije imao alibi za vreme napada",
                  "Tehnički dokazi nisu neophodni — dovoljno je motiv i prilika"],
                 1, "Sajber predmeti zahtevaju višeslojan dokazni materijal — samo forenzički dokazi sa chain of custody su prihvatljivi."],
                ["Kako koordinisati istragu sa stranim agencijama (FBI, Europol) bez kompromitovanja tajnosti?",
                 ["Podeliti sve detalje istrage sa svim partnerima odmah za maksimalnu koordinaciju",
                  "Strukturisana razmena kroz formalne kanale uz jasno definisane kategorije informacija koje se dele i koje se čuvaju tajnim",
                  "Odbiti međunarodnu koordinaciju jer bi ugrozila tajnost domaće istrage",
                  "Prepustiti stranim agencijama vođenje istrage jer imaju bolje kapacitete"],
                 1, "Formalna razmena kroz etablirane kanale sa jasnim protokolima štiti tajnost dok omogućava efikasnu koordinaciju."]
            ],
            "Diplomatija": [
                ["OSINT ukazuje na potencijalnu državnu umešanost. Koji je diplomatski protokol za diskretno istraživanje?",
                 ["Odmah pozvati ambasadora sumnjive države na razgovor i konfrontirati ih sa OSINT nalazima",
                  "Diskretni bilateralni kontakti kroz obaveštajne kanale, koordinacija sa saveznicima i prikupljanje nezavisnih potvrda",
                  "Podneti pritužbu Savetu bezbednosti UN na osnovu OSINT analize",
                  "Čekati da sumnjiva država sama prizna umešanost pre preduzimanja diplomatskih koraka"],
                 1, "Diskrecija u ranoj fazi čuva prostor za akciju — javna konfrontacija bez dokaza je diplomatska greška sa trajnim posledicama."],
                ["Saveznici dele sopstvene atribucione procene koje se razlikuju od srpskih. Kako diplomatija postupa?",
                 ["Prihvatiti savezničku procenu bez kritičke analize jer su saveznici pouzdaniji",
                  "Kritički analizovati sve procene, tražiti metodologiju i uskladiti pozicije na osnovu sveukupnih dokaza",
                  "Odbaciti savezničke procene jer ne razumeju srpski kontekst",
                  "Javno objaviti razliku u procenama kao dokaz nezavisnosti srpske politike"],
                 1, "Savezničke procene su vredan input ali ne zamenjuju sopstvenu analizu — usklađivanje pozicija jača kolektivni odgovor."],
                ["Koji je dugoročni diplomatski cilj Srbije u domenu sajber bezbednosti posle ovog incidenta?",
                 ["Razviti sopstvene ofanzivne sajber kapacitete za odvraćanje potencijalnih napadača",
                  "Jačanje bilateralne i multilateralne saradnje, izgradnja kapaciteta i aktivno učešće u normativnim procesima",
                  "Izolacija digitalnih sistema — smanjiti međunarodnu izloženost smanjenjem konekcija",
                  "Prepustiti sajber bezbednost u potpunosti privatnom sektoru koji je fleksibilniji od državnih struktura"],
                 1, "Srbija kao mala zemlja maksimizira uticaj kroz saradnju i normativno angažovanje — izolacija je kontraproduktivna."]
            ]
        }
    },
    {
        "title": "FAZA 6 — Oporavak, lekcije naučene i institucionalni odgovor",
        "clock": "11:15",
        "narrative": "Sistem se postepeno oporavlja. Istraga je u toku. Vlada saziva vanrednu sednicu. EDG priprema After-Action Report. Mediji, regulatori i javnost traže sistemski odgovor i garancije.",
        "visual": "RECOVERY STATUS DASHBOARD\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nPower grid: 87% restored\nSCADA systems: Partially online\nCustomer data: Under forensic review\nForensic investigation: ONGOING\n\nPublic inquiry: REQUESTED\nRegulatory review: INITIATED\nInsurance claim: FILED\n\nNext press conference: 14:00h\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "inject": "Ministar unutrašnjih poslova poziva EDG na hitne konsultacije. Regulatorno telo za energetiku najavljuje inspekciju.",
        "questions": {
            "shared": ["Koji je ključni dokument koji EDG mora pripremiti po završetku incidenta za sve zainteresovane strane?",
                ["Samo finansijski izveštaj o šteti za akcionare i osiguravajuće kuće",
                 "After-Action Report koji obuhvata hronologiju, uzroke, mere odgovora, lekcije naučene i preporuke za unapređenje",
                 "Interni IT izveštaj koji ostaje poverljiv i ne deli se izvan kompanije",
                 "Kratko saopštenje za javnost o uspešnom okončanju incidenta bez tehničkih detalja"],
                1, "After-Action Report je profesionalni standard — sveobuhvatan dokument koji služi kao osnov za unapređenje bezbednosti i transparentnost prema regulatorima."],
            "Izvršni bord direktora": [
                ["Vlada saziva vanrednu sednicu o zaštiti kritične infrastrukture. Uloga EDG-a?",
                 ["Izbeći učešće jer je to politički proces koji ne tiče kompanije direktno",
                  "Aktivno učešće sa konkretnim preporukama zasnovanim na iskustvu iz incidenta",
                  "Prisustvovati ali ne davati preporuke — to nije uloga kompanije",
                  "Slati samo pravni tim jer se radi o regulatornom pitanju"],
                 1, "Kompanija koja je prošla incident ima jedinstven uvid koji je vredan doprinos sistemskim merama — angažovanje je i obaveza i šansa."],
                ["Koje su dugoročne organizacione promene koje Izvršni bord mora sprovesti?",
                 ["Promena PR direktora i IT menadžera kao odgovornih za propuste u komuniciranaju i tehnici",
                  "Uspostavljanje CISO pozicije, kriznog komiteta, godišnjih vežbi i kontinuiranog bezbednosnog programa",
                  "Kupovina novog bezbednosnog softvera je jedina potrebna promena",
                  "Nikakve — incident je bio nepreditkiv i nije indikator sistemskih slabosti"],
                 1, "Incident je razotkriio organizacione slabosti — strukturne promene u upravljanju bezbednošću su jedini odgovarajući odgovor."],
                ["Kako Izvršni bord komunicira prema akcionarima i investitorima posle incidenta?",
                 ["Minimizirati incident u komunikaciji sa akcionarima da se zaštiti vrednost akcija",
                  "Transparentna komunikacija o incidentu, merama odgovora, nastaloj šteti i planovima unapređenja",
                  "Koristiti pravne instrumente da se spreče pitanja akcionara na godišnjoj skupštini",
                  "Akcionari nemaju pravo na informacije o bezbednosnim incidentima jer su to poverljivi operativni podaci"],
                 1, "Akcionari imaju pravo na materijalne informacije — transparentnost smanjuje pravni rizik i gradi dugoročno poverenje."]
            ],
            "Pravni tim": [
                ["Regulatorno telo za energetiku pokrenulo je inspekciju. Kako pravni tim priprema EDG?",
                 ["Odlagati inspekciju što duže i pružati minimalno informacija",
                  "Pripremiti kompletnu dokumentaciju, osigurati transparentnu saradnju i proaktivno prikazati preduzete mere",
                  "Odbiti inspekciju pozivajući se na poverljivost poslovnih podataka",
                  "Prepustiti IT timu da se bavi tehničkim aspektima inspekcije bez pravnog angažovanja"],
                 1, "Saradnja sa regulatorom je zakonska obaveza i strateška mudrost — otpor se tumači kao prikrivanje i povećava sankcije."],
                ["Osiguravajuća kuća traži dokumentaciju za obradu zahteva. Šta je neophodno?",
                 ["Usmeni opis incidenta je dovoljan za obradu zahteva",
                  "Kompletna dokumentacija: forenzički izveštaji, logovi, komunikacija, finansijska procena štete, chain of custody",
                  "Samo finansijska procena štete bez tehničkih detalja o napadu",
                  "Polisa ne pokriva sajber napade pa nije potrebna posebna dokumentacija"],
                 1, "Osiguravajuće kuće zahtevaju sveobuhvatnu dokumentaciju sa chain of custody — nepotpuna dokumentacija je osnov za odbijanje zahteva."],
                ["Pogođeni građani podnose tužbe zbog curenja podataka. Kako pravni tim odgovara?",
                 ["Odbraniti se agresivno negiranjem svake odgovornosti EDG-a",
                  "Proceniti osnovanost svake tužbe, razmotriti vansudsko poravnanje i pripremiti čvrstu pravnu odbranu zasnovanu na preduzetim merama",
                  "Automatski prihvatiti odgovornost da bi se izbeglo suđenje",
                  "Ignorisati tužbe jer su male šanse za uspeh u parnicama vezanim za sajber napade"],
                 1, "Individualizovana procena sa razmatranjem poravnanja i čvrstom odbranom je strateški optimalna pozicija u masovnim tužbama."]
            ],
            "IT/CERT tim": [
                ["Koji su ključni koraci u fazi oporavka sistema pre vraćanja u produkciju?",
                 ["Vratiti sistem u produkciju odmah — svaki minut prekida uzrokuje operativne i finansijske gubitke",
                  "Forenzička provera svih sistema, testiranje backup-a, validacija integriteta, bezbednosni audit i faze vraćanja sa nadzorom",
                  "Kupiti potpuno novi hardver i software jer kompromitovani sistemi ne mogu biti bezbedni",
                  "Oporavak je isključivo odgovornost dobavljača sistema, ne IT/CERT tima"],
                 1, "Oporavak bez testiranja je rizičan — napadač može biti prisutan u backup-u ili sistem može biti nestabilan."],
                ["Koji su ključni bezbednosni unapređenja koje IT/CERT preporučuje posle ovog incidenta?",
                 ["Promena lozinki za sve korisnike i povećanje minimalnog broja karaktera je dovoljna mera",
                  "Zero Trust arhitektura, MFA za sve pristupe, redovno testiranje backup-a, EDR na OT sistemima, 24/7 SOC, vendor risk management",
                  "Kupovina novog NGFW rešenja renomiranog proizvođača kao centralna mera",
                  "Fizičko odvajanje IT i OT mreže kao jedina suštinska i dovoljna mera"],
                 1, "Incident je razotkriven višestruke slabosti — jedna tačkasta mera ne može adresirati sistemski bezbednosni deficit."],
                ["Kako IT/CERT dokumentuje incident za threat intelligence zajednicu i buduće vežbe?",
                 ["Interni izveštaj koji ostaje interno jer su podaci o incidentu poverljivi",
                  "Strukturisani After-Action Report u STIX/TAXII formatu, anonimizovana razmena sa CERT mrežama",
                  "Usmeni brifing za tim koji je reagovao — pisana dokumentacija nije neophodna",
                  "Jedino CEO i Izvršni bord dobijaju izveštaj o incidentu"],
                 1, "STIX/TAXII formati omogućavaju interoperabilnu razmenu threat intelligence sa nacionalnim i međunarodnim CERT mrežama."]
            ],
            "PR tim": [
                ["Incident je okončan. Kako PR gradi pozitivni narativ bez minimizacije ozbiljnosti?",
                 ["'Sve je sjajno — kompanija je jača i bezbednija nego ikad pre incidenta'",
                  "Autentična komunikacija: šta se desilo, kako je rešeno, šta smo naučili i konkretno šta menjamo — bez spin-a",
                  "Ne pričati o incidentu — medijskog interesu nestaje ako kompanija ne daje hrane pažnji",
                  "Nagraditi pozitivno izveštavanje novinara ekskluzivnim intervjuima i informacijama"],
                 1, "Autentičnost bez spin-a dugoročno gradi poverenje — javnost prepoznaje i nagrađuje organizacije koje otvoreno komuniciraju."],
                ["Godinu dana posle incidenta mediji pišu retrospektivu. Kako EDG PR proaktivno upravlja narativom?",
                 ["Odbiti sve retrospektivne upite jer incident treba zaboraviti a ne obnavljati",
                  "Proaktivno ponuditi intervju sa rukovodstvom i prikazati konkretne mere i unapređenja u protekloj godini",
                  "Pustiti da retrospektiva prođe bez reakcije kompanije",
                  "Tužiti medije koji pišu negativne retrospektive kao oblik poslovnog defamiranja"],
                 1, "Retrospektiva je prilika za pozicioniranje kompanije kao organizacije koja uči i unapređuje se — proaktivnost je prednost."],
                ["Kako EDG gradi dugoročno poverenje korisnika nakon ovakvog incidenta?",
                 ["Dati finansijske popuste na računima svim korisnicima kao kompenzaciju",
                  "Redovna transparentna komunikacija o bezbednosnim ulaganjima, godišnji izveštaji i programi za korisnike",
                  "Rebrending kompanije sa novim imenom i logotipom da bi se distancirali od incidenta",
                  "Sponzorisanje popularnih sportskih i kulturnih događaja za poboljšanje javnog imidža"],
                 1, "Suštinska, dugotrajna promena prakse i transparentna komunikacija o njoj jedina gradi autentično poverenje na dugi rok."]
            ],
            "Policija/Tužilaštvo": [
                ["Istraga je završena i sprema se optužnica. Koji su ključni izazovi u suđenju za sajber kriminal?",
                 ["Nema posebnih izazova — sajber dokazi su pravno identični fizičkim dokazima",
                  "Jurisdikcija, autentičnost digitalnih dokaza, pronalazak i izručenje osumnjičenih, tehnička složenost za veće i sudije",
                  "Jedini izazov je presuda — istraga je uvek uspešna ako je profesionalno vođena",
                  "Sajber predmeti se uvek rešavaju nagodbom pre suđenja — suđenja praktično nema"],
                 1, "Sajber predmeti su suštinski složeniji — ekspertski dokazi, jurisdikcija i izručenje su trajne prepreke koje treba adresirati."],
                ["Osumnjičeni su identifikovani ali se nalaze u inostranstvu. Koji su mehanizmi?",
                 ["Nema opcija bez fizičkog prisustva osumnjičenog na teritoriji Srbije",
                  "Poternica, Interpol crvena obaveštenja, zahtev za izručenje kroz bilateralne sporazume, zamrzavanje imovine u trećim državama",
                  "Jedino diplomatski pritisak kroz ambasade — pravni mehanizmi ne funkcionišu u praksi",
                  "Suđenje u odsustvu bez ikakve međunarodne koordinacije je jedina opcija"],
                 1, "Kombinacija Interpola, zahteva za izručenje i zamrzavanja imovine čini kriminal finansijski neatraktivnim čak i bez hapšenja."],
                ["Koje zakonodavne preporuke Tužilaštvo daje posle ovog incidenta?",
                 ["Ništa, jer su sadašnji zakoni potpuno adekvatni i nema potrebe za promenama",
                  "Ažuriranje KZ za AI-asistovana krivična dela, jačanje kapaciteta Tužilaštva za VTK i ubrzanje MLA procedura",
                  "Prebacivanje nadležnosti na civilne sudove koji su dostupniji i brži",
                  "Zakonodavne preporuke nisu uloga tužilaštva — to je isključivo parlamentarna nadležnost"],
                 1, "Tužilaštvo koje je vodilo istragu ima jedinstvene uvide u praznine zakonodavnog okvira — preporuke su legitimna i vredna funkcija."]
            ],
            "Diplomatija": [
                ["Istraga je pokazala TTP overlap sa grupom iz orbite konkretne strane države. Finalna diplomatska preporuka?",
                 ["Odmah prekinuti diplomatske odnose sa tom državom kao signal odlučnosti",
                  "Obavestiti savezničke partnere, koordinisati pozicije i razmotriti ciljane mere srazmerno dokazima",
                  "Privatno optužiti ambasadu te države ali bez ikakvih javnih ili pravnih posledica",
                  "Sačekati da se napadači sami identifikuju pre preduzimanja bilo kakve akcije"],
                 1, "Srazmerne, koordinisane mere sa saveznicima imaju veći efekt od unilateralnih akcija i manje diplomatski rizik."],
                ["Srbija je pozvana da podeli iskustvo na OSCE konferenciji o sajber bezbednosti. Cilj nastupa?",
                 ["Kritizovati konkretnu stranu državu pred međunarodnom publikom koristeći tribinu",
                  "Podeliti lekcije naučene, zagovarati jačanje normi i promovisati međunarodnu saradnju bez prejudiciranja otvorene istrage",
                  "Odbiti nastup jer je incident suviše svež za javnu međunarodnu diskusiju",
                  "Prezentovati samo tehničke detalje napada bez diplomatskog i normativnog konteksta"],
                 1, "Multilateralni forumi su idealan prostor za normativni doprinos bez eksponiranja osetljivih operativnih detalja."],
                ["Koji je dugoročni diplomatski cilj Srbije u domenu sajber bezbednosti posle ovog incidenta?",
                 ["Razviti sopstvene ofanzivne sajber kapacitete za odvraćanje potencijalnih napadača",
                  "Jačanje bilateralne i multilateralne saradnje, izgradnja kapaciteta i aktivno učešće u normativnim procesima",
                  "Izolacija digitalnih sistema — smanjiti međunarodnu izloženost smanjenjem konekcija",
                  "Prepustiti sajber bezbednost u potpunosti privatnom sektoru koji je fleksibilniji od državnih struktura"],
                 1, "Srbija kao mala zemlja maksimizira uticaj kroz saradnju i normativno angažovanje — izolacija je kontraproduktivna."]
            ]
        }
    }
]


PRESETS = {
    "Novinar": [
        "Da li je tačno da su hakeri ugasili struju u gradu?",
        "Da li su podaci građana procureli i u kojoj meri?",
        "Da li je napad povezan sa stranom državom?",
        "Kada se može očekivati oporavak sistema?"
    ],
    "Poverenik": [
        "Dostavite opis incidenta, kategorije podataka i preduzete mere u roku od 2h.",
        "Da li ste obavestili sva pogođena lica o povredi podataka?",
        "Koji je pravni osnov za zadržavanje logova i ko ima pristup?"
    ],
    "Ambasada": [
        "Da li zvanično povezujete incident sa našom državom?",
        "Tražimo zvanični stav vlade zbog medijskih spekulacija.",
        "Zahtevamo konzularne kontakte u slučaju hapšenja naših državljana."
    ],
    "Uprava EDG": [
        "Da li plaćamo otkup — koji je stav kriznog štaba?",
        "Ko odobrava javno saopštenje i kada izlazimo sa izjavom?",
        "Kada možemo garantovati oporavak sistema za korisnike?"
    ],
    "Građani": [
        "Kada se vraća struja u našem delu grada?",
        "Da li su moja adresa i podaci o računu javno dostupni?",
        "Šta treba da preduzmem da zaštitim svoje podatke?"
    ]
}

# ═══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY STATE
# ═══════════════════════════════════════════════════════════════════════════════

STATE = {
    "sessions":      {},   # token -> session dict
    "tokens":        {},   # token -> meta
    "events":        [],   # audit log
    "injects":       [],   # moderator messages
    "responses":     [],   # team responses to injects
    "media_feed":    [],   # live media items
    "team_phases":   {},   # "team::phase" -> {individual, consensus, status}
    "phase_locks":   {},   # team -> max unlocked phase index
}

SESSION_TTL = 6 * 3600

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def make_token():
    return "BG-" + str(uuid.uuid4())[:8].upper()

def tp_key(team, phase_idx):
    return f"{team}::{phase_idx}"

def get_unlock(team):
    return STATE["phase_locks"].get(team, 0)

def log_event(kind, token="", team="", name="", details=""):
    with STATE_LOCK:
        STATE["events"].append({
            "time": now_str(), "kind": kind, "token": token,
            "team": team, "name": name, "details": str(details)[:256]
        })
        if len(STATE["events"]) > 5000:
            STATE["events"] = STATE["events"][-5000:]

def trim_state():
    now = time.time()
    with STATE_LOCK:
        expired = [t for t, s in STATE["sessions"].items()
                   if now - s.get("started_at", now) > SESSION_TTL]
        for t in expired:
            STATE["sessions"].pop(t, None)
            STATE["tokens"].pop(t, None)
        if len(STATE["media_feed"]) > 200:
            STATE["media_feed"] = STATE["media_feed"][-200:]

# WebSocket connections for live push
class WSManager:
    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, token: str, ws: WebSocket):
        await ws.accept()
        self.active[token] = ws

    def disconnect(self, token: str):
        self.active.pop(token, None)

    async def broadcast_mod(self):
        dead = []
        for tok, ws in list(self.active.items()):
            meta = STATE["tokens"].get(tok, {})
            if meta.get("mode") == "moderator":
                try:
                    await ws.send_json({"type": "mod_refresh"})
                except:
                    dead.append(tok)
        for d in dead:
            self.disconnect(d)

    async def push_to(self, token: str, data: dict):
        ws = self.active.get(token)
        if ws:
            try:
                await ws.send_json(data)
            except:
                self.disconnect(token)

ws_manager = WSManager()

# ═══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="Operation Black Grid")

# Resolve paths relative to this file — works both locally and in Azure /tmp/... paths
_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR    = os.path.join(_BASE_DIR, "static")
_TEMPLATES_DIR = os.path.join(_BASE_DIR, "templates")

# Auto-create static dir: Azure ZIP deploy strips empty directories from the archive
os.makedirs(_STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ─── STARTUP ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    db_init()
    db_load()
    atexit.register(db_save)

    def _periodic():
        while True:
            time.sleep(30)
            trim_state()
            db_save()
    threading.Thread(target=_periodic, daemon=True).start()
    print(f"[BLACKGRID] FastAPI started. PID={os.getpid()} DB={DB_PATH}")

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_session(token: str):
    return STATE["sessions"].get((token or "").strip().upper())

def get_token_meta(token: str):
    return STATE["tokens"].get((token or "").strip().upper())

def require_mod(token: str):
    meta = get_token_meta(token)
    if not meta or meta.get("mode") != "moderator":
        raise HTTPException(403, "Nemate moderatorska prava.")

# ─── ROUTES: PAGES ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "teams": TEAMS})

@app.get("/play", response_class=HTMLResponse)
async def play(request: Request, token: str = ""):
    s = get_session(token)
    if not s:
        return RedirectResponse("/")
    phase_idx = s.get("phase", 0)
    unlocked  = get_unlock(s["team"])
    phase     = PHASES[phase_idx] if phase_idx < len(PHASES) else None
    tp        = STATE["team_phases"].get(tp_key(s["team"], phase_idx), {})
    return templates.TemplateResponse("play.html", {
        "request":     request,
        "token":       token.upper(),
        "session":     s,
        "phase":       phase,
        "phase_idx":   phase_idx,
        "phase_count": len(PHASES),
        "unlocked":    unlocked,
        "tp":          tp,
        "role":        ROLE_CARDS.get(s["team"], {}),
        "phases_json": json.dumps(PHASES, ensure_ascii=False),
    })

@app.get("/mod", response_class=HTMLResponse)
async def mod_panel(request: Request, token: str = ""):
    meta = get_token_meta(token)
    if not meta or meta.get("mode") != "moderator":
        return RedirectResponse("/")
    return templates.TemplateResponse("mod.html", {
        "request":      request,
        "token":        token.upper(),
        "teams":        TEAMS,
        "phases":       PHASES,
        "presets":      PRESETS,
        "presets_json": json.dumps(PRESETS, ensure_ascii=False),
    })

# ─── AUTH ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    name: str
    team: str
    mode: str
    exercise_code: str = ""
    password: str = ""

@app.post("/api/login")
async def api_login(req: Request, body: LoginRequest):
    ip = req.client.host if req.client else "unknown"
    if not rate_ok(ip):
        return JSONResponse({"ok": False, "error": "Previše pokušaja. Sačekajte 60 sekundi."}, 429)

    if body.mode == "moderator":
        if not check_mod(body.password):
            return JSONResponse({"ok": False, "error": "Pogrešna moderator lozinka."})
        token = make_token()
        with STATE_LOCK:
            STATE["tokens"][token] = {"mode": "moderator", "name": body.name or "Moderator"}
        log_event("MOD_LOGIN", token, "Moderator", body.name or "Moderator")
        return JSONResponse({"ok": True, "token": token, "mode": "moderator", "redirect": f"/mod?token={token}"})

    # Participant
    if not body.name or not body.team or body.team not in TEAMS:
        return JSONResponse({"ok": False, "error": "Unesite ime i izaberite tim."})
    if not check_participant(body.exercise_code):
        return JSONResponse({"ok": False, "error": "Pogrešan kod vežbe."})
    token = make_token()
    with STATE_LOCK:
        STATE["tokens"][token] = {"mode": "student", "name": body.name, "team": body.team}
        STATE["sessions"][token] = {
            "name": body.name, "team": body.team, "phase": 0, "score": 0,
            "decisions": {}, "started_at": time.time(), "phase_started_at": None,
        }
    log_event("STUDENT_LOGIN", token, body.team, body.name)
    return JSONResponse({"ok": True, "token": token, "mode": "student", "redirect": f"/play?token={token}"})

@app.post("/api/rejoin")
async def api_rejoin(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = STATE["sessions"].get(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Token nije pronađen."})
    log_event("REJOIN", token, s["team"], s["name"])
    return JSONResponse({"ok": True, "token": token, "redirect": f"/play?token={token}"})

# ─── PARTICIPANT APIs ──────────────────────────────────────────────────────────

@app.post("/api/start_phase")
async def api_start_phase(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Sesija nije aktivna."})
    phase_idx = s.get("phase", 0)
    unlocked  = get_unlock(s["team"])
    if phase_idx > unlocked:
        return JSONResponse({"ok": False, "error": "Faza još nije otključana."})
    with STATE_LOCK:
        s["phase_started_at"] = time.time()
        key = tp_key(s["team"], phase_idx)
        if key not in STATE["team_phases"]:
            STATE["team_phases"][key] = {"individual": {}, "consensus": [], "status": "active"}
    log_event("START_PHASE", token, s["team"], s["name"], f"F{phase_idx+1}")
    return JSONResponse({"ok": True})

@app.post("/api/submit_individual")
async def api_submit_individual(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Sesija nije aktivna."})
    answers = body.get("answers", [])
    if len(answers) != 4 or any(not a for a in answers):
        return JSONResponse({"ok": False, "error": "Odgovorite na sva četiri pitanja."})
    phase_idx = s.get("phase", 0)
    key = tp_key(s["team"], phase_idx)
    with STATE_LOCK:
        tp = STATE["team_phases"].setdefault(key, {"individual": {}, "consensus": [], "status": "active"})
        tp["individual"][token] = answers
    log_event("INDIVIDUAL_SUBMIT", token, s["team"], s["name"], f"F{phase_idx+1}")
    # Count team members
    with STATE_LOCK:
        members = [t for t, ss in STATE["sessions"].items() if ss["team"] == s["team"]]
        submitted = list(tp["individual"].keys())
    return JSONResponse({"ok": True, "submitted": len(submitted), "total": len(members)})

@app.post("/api/submit_consensus")
async def api_submit_consensus(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Sesija nije aktivna."})
    answers = body.get("answers", [])
    if len(answers) != 4 or any(not a for a in answers):
        return JSONResponse({"ok": False, "error": "Odgovorite na sva četiri pitanja."})
    phase_idx = s.get("phase", 0)
    key = tp_key(s["team"], phase_idx)
    p  = PHASES[phase_idx]
    qs = p["questions"]
    shared   = qs["shared"]
    team_qs  = qs.get(s["team"], [["—", ["—"], 0, "—"]] * 3)
    all_q    = [shared] + list(team_qs)

    gained = 0
    results = []
    for i, q in enumerate(all_q):
        correct = q[1][q[2]]
        ok      = answers[i] == correct
        if ok:
            gained += 10
        results.append({"question": q[0], "selected": answers[i], "correct": correct, "ok": ok, "explain": q[3]})

    with STATE_LOCK:
        tp = STATE["team_phases"].setdefault(key, {"individual": {}, "consensus": [], "status": "active"})
        tp["consensus"] = answers
        tp["status"]    = "done"
        # Score all team members
        for t, ss in STATE["sessions"].items():
            if ss["team"] == s["team"]:
                ss["score"] += gained
                for i, q in enumerate(all_q):
                    ss["decisions"][f"f{phase_idx+1}q{i+1}"] = {
                        "ok": results[i]["ok"],
                        "selected": answers[i],
                        "correct": results[i]["correct"],
                        "question": q[0]
                    }
    log_event("CONSENSUS_SUBMIT", token, s["team"], s["name"], f"F{phase_idx+1} skor:{gained}")
    threading.Thread(target=db_save, daemon=True).start()
    return JSONResponse({"ok": True, "gained": gained, "max": len(all_q) * 10, "results": results})

@app.get("/api/phase_state")
async def api_phase_state(token: str):
    token = token.strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False})
    phase_idx = s.get("phase", 0)
    unlocked  = get_unlock(s["team"])
    key       = tp_key(s["team"], phase_idx)
    tp        = STATE["team_phases"].get(key, {})
    members   = [t for t, ss in STATE["sessions"].items() if ss["team"] == s["team"]]
    submitted = list(tp.get("individual", {}).keys())
    elapsed   = int(time.time() - s["phase_started_at"]) if s.get("phase_started_at") else 0
    return JSONResponse({
        "ok":       True,
        "phase":    phase_idx,
        "unlocked": unlocked,
        "status":   tp.get("status", "idle"),
        "submitted_count": len(submitted),
        "total_members":   len(members),
        "elapsed":         elapsed,
        "score":           s["score"],
        "next_available":  unlocked > phase_idx and tp.get("status") == "done",
        "session_expired": False,
    })

@app.post("/api/next_phase")
async def api_next_phase(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Sesija nije aktivna."})
    phase_idx = s.get("phase", 0)
    unlocked  = get_unlock(s["team"])
    next_idx  = phase_idx + 1
    if next_idx > unlocked or next_idx >= len(PHASES):
        return JSONResponse({"ok": False, "error": "Sledeća faza nije dostupna."})
    with STATE_LOCK:
        s["phase"]           = next_idx
        s["phase_started_at"] = None
        key = tp_key(s["team"], next_idx)
        if key not in STATE["team_phases"]:
            STATE["team_phases"][key] = {"individual": {}, "consensus": [], "status": "active"}
    log_event("NEXT_PHASE", token, s["team"], s["name"], f"F{next_idx+1}")
    return JSONResponse({"ok": True, "phase": next_idx})

@app.get("/api/injects")
async def api_injects(token: str):
    token = token.strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse([])
    relevant = [x for x in STATE["injects"]
                if x["target"] in ["Svi timovi", s["team"], s["name"], token]]
    return JSONResponse(relevant[-15:])

@app.post("/api/respond_inject")
async def api_respond_inject(body: dict):
    token = str(body.get("token", "")).strip().upper()
    s = get_session(token)
    if not s:
        return JSONResponse({"ok": False, "error": "Sesija nije aktivna."})
    inject_id = body.get("inject_id", "").strip()
    text      = body.get("text", "").strip()
    if not inject_id or not text:
        return JSONResponse({"ok": False, "error": "Unesite ID poruke i odgovor."})
    with STATE_LOCK:
        STATE["responses"].append({
            "time": now_str(), "ts": time.time(),
            "inject_id": inject_id, "token": token,
            "team": s["team"], "name": s["name"], "text": text
        })
    log_event("INJECT_RESPONSE", token, s["team"], s["name"], f"{inject_id}: {text[:80]}")
    return JSONResponse({"ok": True})

@app.get("/api/media_feed")
async def api_media_feed(token: str):
    token = token.strip().upper()
    s = get_session(token)
    feed = STATE["media_feed"][-15:] if s else []
    return JSONResponse(feed)

# ─── MODERATOR APIs ────────────────────────────────────────────────────────────

@app.get("/api/mod/dashboard")
async def api_mod_dashboard(token: str):
    require_mod(token)
    groups = {}
    for tok, s in STATE["sessions"].items():
        groups.setdefault(s["team"], []).append((tok, s))

    teams_data = []
    for team in TEAMS:
        members = groups.get(team, [])
        if not members:
            continue
        unlocked = get_unlock(team)
        phases_done = []
        for i in range(len(PHASES)):
            tp = STATE["team_phases"].get(tp_key(team, i), {})
            phases_done.append(tp.get("status") == "done")
        current_phase = max((s.get("phase", 0) for _, s in members), default=0)
        tp = STATE["team_phases"].get(tp_key(team, current_phase), {})
        ind_count = len(tp.get("individual", {}))
        consensus_done = tp.get("status") == "done"
        team_score = sum(s["score"] for _, s in members)
        teams_data.append({
            "team": team,
            "icon": ROLE_CARDS[team]["icon"],
            "members": [{"name": s["name"], "token": tok, "score": s["score"],
                         "submitted": tok in tp.get("individual", {})} for tok, s in members],
            "current_phase": current_phase,
            "unlocked": unlocked,
            "ind_count": ind_count,
            "consensus_done": consensus_done,
            "team_score": team_score,
            "phases_done": phases_done,
            "can_unlock": consensus_done and current_phase < len(PHASES) - 1,
        })

    stats = {
        "sessions": len(STATE["sessions"]),
        "teams": len(groups),
        "injects": len(STATE["injects"]),
        "responses": len(STATE["responses"]),
    }
    return JSONResponse({"teams": teams_data, "stats": stats})

@app.post("/api/mod/unlock")
async def api_mod_unlock(body: dict):
    token = str(body.get("token", "")).strip().upper()
    require_mod(token)
    team = body.get("team", "")
    if team not in TEAMS:
        return JSONResponse({"ok": False, "error": "Nevalidan tim."})
    members = [s for s in STATE["sessions"].values() if s["team"] == team]
    if not members:
        return JSONResponse({"ok": False, "error": "Tim nema prijavljenih članova."})
    current = max(s.get("phase", 0) for s in members)
    tp = STATE["team_phases"].get(tp_key(team, current), {})
    if tp.get("status") != "done":
        return JSONResponse({"ok": False, "error": "Tim nije završio tekuću fazu."})
    if current + 1 >= len(PHASES):
        return JSONResponse({"ok": False, "error": "Nema više faza."})
    with STATE_LOCK:
        STATE["phase_locks"][team] = current + 1
    log_event("MOD_UNLOCK", token, team, "Moderator", f"Otključana F{current+2}")
    threading.Thread(target=db_save, daemon=True).start()
    return JSONResponse({"ok": True, "unlocked": current + 1})

@app.post("/api/mod/unlock_all")
async def api_mod_unlock_all(body: dict):
    token = str(body.get("token", "")).strip().upper()
    require_mod(token)
    phase_idx = int(body.get("phase", 0))
    with STATE_LOCK:
        for team in TEAMS:
            STATE["phase_locks"][team] = phase_idx
    log_event("MOD_UNLOCK_ALL", token, "ALL", "Moderator", f"Sve faze na {phase_idx+1}")
    return JSONResponse({"ok": True})

@app.post("/api/mod/inject")
async def api_mod_inject(body: dict):
    token = str(body.get("token", "")).strip().upper()
    require_mod(token)
    msg    = str(body.get("msg", "")).strip()
    target = str(body.get("target", "Svi timovi"))
    sender = str(body.get("sender", "Moderator"))
    deadline = int(body.get("deadline", 5))
    if not msg:
        return JSONResponse({"ok": False, "error": "Unesite poruku."})
    inj = {
        "id": str(uuid.uuid4())[:6], "time": now_str(), "ts": time.time(),
        "sender": sender, "target": target, "msg": msg, "deadline": deadline
    }
    with STATE_LOCK:
        STATE["injects"].append(inj)
    log_event("MOD_INJECT", token, target, sender, f"{inj['id']}: {msg[:80]}")
    return JSONResponse({"ok": True, "id": inj["id"]})

@app.post("/api/mod/breaking")
async def api_mod_breaking(body: dict):
    token = str(body.get("token", "")).strip().upper()
    require_mod(token)
    text    = str(body.get("text", "")).strip()
    channel = str(body.get("channel", "MODERATOR"))
    target  = str(body.get("target", "Svi"))
    if not text:
        return JSONResponse({"ok": False, "error": "Unesite tekst."})
    item = {
        "id": f"br-{uuid.uuid4().hex[:6]}", "time": now_str(), "ts": time.time(),
        "icon": "🚨", "channel": channel, "text": text,
        "kind": "breaking", "target_team": target if target != "Svi" else None,
        "requires_response": True, "responded_by": None, "ignored": False,
    }
    with STATE_LOCK:
        STATE["media_feed"].append(item)
    log_event("MOD_BREAKING", token, target, channel, text[:80])
    return JSONResponse({"ok": True, "id": item["id"]})

@app.get("/api/mod/events")
async def api_mod_events(token: str):
    require_mod(token)
    return JSONResponse(STATE["events"][-100:])

@app.get("/api/mod/responses")
async def api_mod_responses(token: str):
    require_mod(token)
    return JSONResponse(STATE["responses"][-50:])

@app.post("/api/mod/reset")
async def api_mod_reset(body: dict):
    token   = str(body.get("token", "")).strip().upper()
    confirm = str(body.get("confirm", ""))
    require_mod(token)
    if confirm != "RESET":
        return JSONResponse({"ok": False, "error": "Unesite RESET za potvrdu."})
    with STATE_LOCK:
        for k in ["sessions", "tokens", "events", "injects", "responses", "media_feed", "team_phases", "phase_locks"]:
            STATE[k].clear() if isinstance(STATE[k], (dict, list)) else None
        # Keep mod token
        STATE["tokens"][token] = {"mode": "moderator", "name": "Moderator"}
    threading.Thread(target=db_save, daemon=True).start()
    return JSONResponse({"ok": True})

@app.get("/api/mod/scoreboard")
async def api_mod_scoreboard(token: str):
    require_mod(token)
    teams = {}
    for s in STATE["sessions"].values():
        t = s["team"]
        if t not in teams:
            teams[t] = {"team": t, "icon": ROLE_CARDS[t]["icon"], "score": 0, "members": 0, "correct": 0, "total_q": 0}
        teams[t]["score"]   += s["score"]
        teams[t]["members"] += 1
        for d in s["decisions"].values():
            teams[t]["total_q"] += 1
            if d.get("ok"):
                teams[t]["correct"] += 1
    result = sorted(teams.values(), key=lambda x: -x["score"])
    return JSONResponse(result)

# ─── WEBSOCKET ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    token = token.strip().upper()
    meta  = get_token_meta(token)
    if not meta:
        await websocket.close(code=4001)
        return
    await ws_manager.connect(token, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(token)
    except Exception:
        ws_manager.disconnect(token)
