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
        "narrative": "EDG, operater elektrodistributivnog sistema i deo kritične infrastrukture, prijavljuje ozbiljan poremećaj. Dispečerski centar gubi kontrolu. Jedan deo grada ostaje bez struje. Na OT/SCADA radnim stanicama pojavljuje se ransomware poruka.",
        "visual": "CRITICAL SYSTEM FAILURE\n\nYour SCADA systems are encrypted.\nPower distribution is disrupted.\nWe have exfiltrated customer personal data,\nbilling information and operational documents.\n\nTime remaining: 72:00:00",
        "inject": "Građani prijavljuju da semafori ne rade. Mediji pitaju da li je kvar ili sajber napad.",
        "questions": {
            "shared": ["Koji je prvi prioritet kriznog štaba u trenutku potvrde sajber napada na kritičnu infrastrukturu?",
                ["Odmah obavestiti medije da se spreči panika",
                 "Stabilizovati distribuciju struje i sprečiti širenje incidenta",
                 "Sačekati kompletnu tehničku analizu pre bilo kakve akcije",
                 "Kontaktirati napadače radi pregovora"],
                1, "Kontinuitet usluge i zaustavljanje širenja uvek imaju prednost u prvim minutima krize."],
            "Izvršni bord direktora": [
                ["Ko ima ovlašćenje da proglasi krizno stanje i predsedava kriznim štabom?",
                 ["Svaki rukovodilac samostalno prema proceni",
                  "Generalni direktor ili ovlašćeno lice prema kriznom protokolu kompanije",
                  "IT menadžer kao jedini tehnički ekspert",
                  "Portparol prema medijima"],
                 1, "Krizni protokol mora jasno definisati liniju komandovanja."],
                ["Da li EDG sme nastaviti isporuku struje kompromitovanim segmentima dok traje incident?",
                 ["Da, svaki prekid je veća šteta od bezbednosnog rizika",
                  "Ne, kompromitovani segmenti moraju biti izolovani čak i po cenu privremenog prekida",
                  "Da, napadači ionako već imaju pristup pa izolacija nema smisla",
                  "Odluku donosi samo regulatorno telo, ne uprava"],
                 1, "Izolacija kompromitovanih segmenata sprečava lateralno kretanje napada."],
                ["Koji organ vlasti EDG mora odmah obavestiti o incidentu koji ugrožava kritičnu infrastrukturu?",
                 ["Samo internu reviziju kompanije",
                  "Nadležni CERT, organ za bezbednost informacija i po potrebi policiju",
                  "Nikoga dok se ne utvrdi potpuna razmera incidenta",
                  "Samo akcionare i investitore"],
                 1, "Zakonska obaveza obaveštavanja nadležnih organa nastaje u trenutku saznanja za incident."]
            ],
            "Pravni tim": [
                ["Koji je zakonski rok za obaveštavanje nadležnih organa o incidentu po Zakonu o informacionoj bezbednosti?",
                 ["72 sata od otkrivanja incidenta",
                  "Odmah, bez nepotrebnog odlaganja — rok od 24h za inicijalni izveštaj",
                  "30 dana od otkrivanja incidenta",
                  "Rok ne postoji, obaveštavanje je opciono"],
                 1, "ZIB propisuje hitno obaveštavanje — svako odlaganje može biti osnov za kaznenu odgovornost."],
                ["Ko je nadležan za prijem prijave incidenta u oblasti kritične infrastrukture?",
                 ["Samo policija MUP-a Srbije",
                  "RATEL kao nacionalno telo za bezbednost mreža i informacionih sistema",
                  "Poverenik za informacije od javnog značaja",
                  "Ministarstvo finansija kao vlasnik javnih preduzeća"],
                 1, "RATEL prima prijave incidenata za IKT sisteme od posebnog značaja."],
                ["Da li EDG u ovom trenutku ima obavezu čuvanja digitalnih tragova incidenta?",
                 ["Ne, fokus je na obnavljanju sistema, ne na čuvanju logova",
                  "Da, digitalni dokazi moraju biti sačuvani u originalnom stanju pre svake tehničke intervencije",
                  "Samo ako policija izda nalog za čuvanje podataka",
                  "Obaveza nastaje tek posle prijave Povereniku"],
                 1, "Uništavanje ili menjanje digitalnih dokaza pre istrage je procesni prekršaj i može uticati na optužnicu."]
            ],
            "IT/CERT tim": [
                ["Koji je prvi tehnički korak IT/CERT tima po potvrdi ransomware infekcije na OT sistemu?",
                 ["Odmah pokrenuti antivirusni scan celog sistema",
                  "Izolovati zaražene segmente od ostatka mreže i interneta, sačuvati forenzičku sliku",
                  "Restartovati sve servere da se malware izbriše iz memorije",
                  "Platiti otkup da bi se dobio ključ za dekripciju"],
                 1, "Izolacija i forenzička slika su prioritet — restart briše in-memory artefakte koji su ključni za istragu."],
                ["Backup nije testiran 6 meseci. Koji je ispravan postupak pre pokušaja oporavka sistema?",
                 ["Odmah koristiti backup bez testiranja — svaki minut prekida korisnicima košta",
                  "Testirati integritet i konzistentnost backup-a u izolovanom okruženju pre primene na produkciju",
                  "Proglasiti backup neupotrebljivim i kupiti novi hardver",
                  "Kontaktirati vendora SCADA sistema i čekati njihove instrukcije"],
                 1, "Netestiran backup može biti kompromitovan ili oštećen — primena na produkciju bez testiranja može produbiti incident."],
                ["Kako forenzički sačuvati volatile podatke (RAM, mrežne konekcije) pre gašenja sistema?",
                 ["Isključiti struju da se sistem odmah zaustavi i sačuva u tekućem stanju",
                  "Napraviti RAM dump i zabeležiti aktivne mrežne konekcije pre isključivanja",
                  "Ostaviti sistem uključenim i sačekati da se malware sam zaustavi",
                  "Volatile podaci nisu korisni za istragu — fokusirati se na disk forenziku"],
                 1, "RAM dump sadrži ključne artefakte: ključeve dekripcije, aktivne procese, mrežne konekcije napadača."]
            ],
            "PR tim": [
                ["Mediji pozivaju za komentar 30 minuta po početku incidenta. Šta PR tim saopštava?",
                 ["Sve tehničke detalje koji su trenutno dostupni da se pokaže transparentnost",
                  "Potvrdu da je incident u toku i da se aktivno radi na rešavanju — bez tehničkih detalja",
                  "Negiranje incidenta dok se ne utvrdi potpuna slika",
                  "Upućivanje na policiju kao jedini izvor informacija"],
                 1, "Rano, kontrolisano saopštavanje sprečava informacioni vakuum koji popunjavaju dezinformacije."],
                ["Koji kanali komunikacije su prioritetni u prvom satu krize?",
                 ["Samo tradicionalni mediji — TV i radio",
                  "Vlastiti digitalni kanali (web, društvene mreže) + zvanično saopštenje za medije",
                  "Isključivo direktna komunikacija sa Vladom — bez javnih saopštenja",
                  "Nema prioriteta — sve informacije se dele istovremeno svim kanalima"],
                 1, "Vlastiti kanali daju kontrolu nad narativom; zvanično saopštenje daje autoritet."],
                ["Šta uraditi ako novinar objavi neproverenu informaciju o broju ugroženih korisnika?",
                 ["Tužiti novinara za narušavanje ugleda kompanije",
                  "Brzo i jasno demantovati konkretnu neistinu i ponuditi proverene podatke",
                  "Ignorisati — odgovor samo amplifikuje lošu priču",
                  "Kontaktirati vlasnike medija i tražiti uklanjanje teksta"],
                 1, "Brz, činjenički odgovor je efikasniji od ignorisanja ili pravnih pretnji u kriznoj komunikaciji."]
            ],
            "Policija/Tužilaštvo": [
                ["Koja je nadležnost tužilaštva u prvom satu po prijavi ransomware napada na kritičnu infrastrukturu?",
                 ["Samo evidentirati prijavu i sačekati da IT tim završi tehničku analizu",
                  "Otvoriti predmet, obezbediti digitalne dokaze i koordinisati sa CERT-om pre tehničke intervencije",
                  "Odmah uhapsiti IT administratore kao potencijalne osumnjičene",
                  "Prepustiti ceo slučaj policiji — tužilaštvo nema ulogu u ovoj fazi"],
                 1, "Rano angažovanje tužilaštva obezbeđuje pravnu validnost prikupljenih dokaza."],
                ["Kako kvalifikovati krivično delo ransomware napada na OT/SCADA sistem elektrodistributivne kompanije?",
                 ["Računarska sabotaža bez otežavajućih okolnosti",
                  "Računarska sabotaža na računarskom sistemu od posebnog značaja — teži oblik dela",
                  "Prevara u privrednom poslovanju",
                  "Krađa elektrane"],
                 1, "Napad na kritičnu infrastrukturu kvalifikuje se kao teži oblik računarske sabotaže sa strožim kaznama."],
                ["Koji međunarodni instrument se aktivira za hitno čuvanje digitalnih podataka kod stranog cloud provajdera?",
                 ["Direktan email stranom provajderu — brže od formalnih mehanizama",
                  "Budimpeštanska konvencija čl. 29 — hitna procedura čuvanja podataka",
                  "GDPR zahtev koji se primenjuje na sve provajdere",
                  "Ne postoji instrument za ovo — sudska nadležnost je apsolutna prepreka"],
                 1, "Čl. 29 Konvencije o sajber kriminalu omogućava hitno čuvanje pre nego što istekne rok čuvanja logova."]
            ],
            "Diplomatija": [
                ["Phishing email dolazi sa servera registrovanog u inostranstvu. Da li to implicira državnu umešanost?",
                 ["Da, server u inostranstvu direktno ukazuje na državni akter te države",
                  "Ne nužno, privatni akteri i kriminalne grupe rutinski koriste infrastrukturu u raznim jurisdikcijama",
                  "Da, posebno ako je server u državi poznatoj po sajber napadima",
                  "Ne postoji veza između geografske lokacije servera i atribucije napadača"],
                 1, "Napadači svesno koriste infrastrukturu u trećim državama upravo da bi otežali atribuciju."],
                ["Saveznici nude obaveštajne podatke o sličnim napadima. Kako se to prima?",
                 ["Direktno od ambasadora na bilateralnom neformalnom sastanku za brzinu",
                  "Kroz zvanične obaveštajne i diplomatske kanale uz formalni zapis o primljenim podacima i metodologiji",
                  "Neformalno putem ličnih kontakata da bi se izbegla birokratska procedura",
                  "Srbija po principu ne prima obaveštajne podatke od saveznika o sajber pretnjama"],
                 1, "Formalni kanali i dokumentovanje su neophodni — neformalne razmene ne mogu biti korišćene u pravnim procedurama."],
                ["Incident privlači pažnju međunarodnih medija i ambasada. Koji je primarni zadatak diplomatije?",
                 ["Publično optužiti strane aktere pre završetka istrage da bi se pokazala odlučnost",
                  "Koordinisati komunikaciju sa saveznicima, sprečiti pogrešnu atribuciju i pratiti diplomatske reakcije",
                  "Predložiti prekid diplomatskih odnosa sa svim sumnjivim državama",
                  "Diplomatija nema ulogu dok istraga nije formalno završena"],
                 1, "Upravljanje međunarodnim narativom sprečava diplomatske incidente bazirane na pogrešnim pretpostavkama."]
            ]
        }
    },
    {
        "title": "FAZA 2 — Finansijska prevara i CEO fraud pokušaj",
        "clock": "09:15",
        "narrative": "Dok traje incident, CFO prima email koji izgleda kao hitni nalog generalnog direktora za hitni transfer sredstava. Deepfake video poziv potvrđuje identitet CEO-a. Paralelno, nepoznate osobe pokušavaju da uđu u server room.",
        "visual": "FROM: ceo@edg-company.net\nTO: cfo@edg-company.net\nSUBJECT: HITNO - Transfer sredstava\n\nPotreban je hitan transfer od 2.3M EUR\nna account: RS35105008123456789\nBEZ OBZIRA na procedure. Sada.\n\nGD Marković\n[DEEPFAKE VIDEO ATTACHED]",
        "inject": "Banka zove CFO-a da potvrdi transfer. Istovremeno, obezbeđenje prijavljuje sumnjive osobe.",
        "questions": {
            "shared": ["Koji je pouzdan način verifikacije autentičnosti hitnog finansijskog naloga tokom krizne situacije?",
                ["Verifikovati putem email odgovora na isti nalog",
                 "Telefonski kontaktirati direktora na prethodno poznat broj i verifikovati po unapred dogovorenoj proceduri",
                 "Prihvatiti ako je video poziv vizuelno ubedljiv",
                 "Hitne situacije opravdavaju zaobilaženje procedura verifikacije"],
                1, "CEO fraud i deepfake napadi su najefikasniji u kriznim situacijama — procedura verifikacije mora biti automatizam."],
            "Izvršni bord direktora": [
                ["Koja je prva akcija Izvršnog borda po saznanju za pokušaj CEO frauda?",
                 ["Izvršiti transfer da se pokaže da kompanija funkcioniše normalno",
                  "Odmah blokirati svaki transfer, obavestiti banku i pokrenuti internu istragu",
                  "Sačekati da CFO sam proceni autentičnost",
                  "Kontaktirati napadače i pregovarati"],
                 1, "Svaka sekunda računica u CEO fraud — banka može da zaustavi transfer samo pre knjiženja."],
                ["Da li krizna situacija opravdava zaobilaženje standardnih finansijskih procedura?",
                 ["Da, kriza zahteva brze odluke bez birokratije",
                  "Ne, krizne situacije su upravo kontekst u kom napadači računaju na pritisak i ubrzano postupanje",
                  "Zavisi od iznosa — manje transakcije mogu ići bez procedure",
                  "Procedure se primenjuju samo u normalnim okolnostima"],
                 1, "Social engineering i CEO fraud su najefikasniji kada je organizacija pod stresom — procedure su tada najvažnije."],
                ["Ko je odgovoran za odobravanje izuzetnih finansijskih transakcija u odsustvu generalnog direktora?",
                 ["Svako od članova borda može da odobri po sopstvenoj proceni",
                  "Unapred definisan zamenik sa jasnim ovlašćenjima prema kriznom planu",
                  "CFO automatski preuzima sva ovlašćenja u krizi",
                  "Niko — transakcije se zamrzavaju do povratka generalnog direktora"],
                 1, "Krizni plan mora jasno definisati sukcesiju ovlašćenja — improvizacija u krizi vodi grešci."]
            ],
            "Pravni tim": [
                ["Koji je pravni status deepfake videa kao dokaza u krivičnom postupku?",
                 ["Deepfake video nije dozvoljen dokaz jer je lažan",
                  "Može biti dokaz o pokušaju prevare ako se autentičnost analize potvrdi veštačenjem",
                  "Video je uvek validan dokaz bez dodatne verifikacije",
                  "Pravni tim nema nadležnost da procenjuje digitalne dokaze"],
                 1, "Deepfake video je dokaz o samom napadu — veštačenje potvrđuje da je lažan, što je esencijalni element bića dela."],
                ["Da li pokušaj CEO frauda predstavlja posebno krivično delo ili deo ransomware napada?",
                 ["Ista kriminalna grupa, isti napad — jedno krivično delo",
                  "Potencijalno posebno krivično delo prevare koje se može goniti paralelno i odvojeno",
                  "CEO fraud nije krivično delo ako transfer nije izvršen",
                  "Krivična odgovornost postoji samo ako je banka izvršila transfer"],
                 1, "Pokušaj prevare je krivično delo bez obzira na uspešnost — paralelna istraga povećava šanse za identifikaciju napadača."],
                ["Koji je rok za prijavljivanje pokušaja finansijske prevare Narodnoj banci Srbije?",
                 ["30 dana od otkrivanja pokušaja",
                  "Odmah — NBS ima sistem za hitno prijavljivanje finansijskih prevara",
                  "Rok ne postoji — prijavljivanje je opciono",
                  "Samo ako je transfer izvršen — pokušaji se ne prijavljuju"],
                 1, "NBS koordiniše sa bankama u realnom vremenu — hitna prijava može sprečiti transfer i kod drugih žrtava iste grupe."]
            ],
            "IT/CERT tim": [
                ["Kako tehnički detektovati deepfake video u realnom vremenu?",
                 ["Vizuelno — ako izgleda realno, verovatno je autentičan",
                  "Analizom metapodataka, artefakata kompresije i biometrijskim alatima za detekciju deepfake-a",
                  "Deepfake detection nije moguć u realnom vremenu",
                  "Pitati prikazanu osobu nešto što samo ona zna"],
                 1, "Specijalizovani deepfake detection alati analiziraju artefakte koji su nevidljivi ljudskom oku."],
                ["Fizički pokušaj upada u server room tokom sajber napada — šta to sugeriše?",
                 ["Slučajnost — fizička i sajber bezbednost su odvojene sfere",
                  "Koordinisan napad koji kombinuje sajber i fizičke vektore — visoko sofisticiran napadač",
                  "Insider threat — neko od zaposlenih je napadač",
                  "Redovna krađa opreme — nema veze sa ransomware napadom"],
                 1, "Koordinacija sajber i fizičkih napada karakteriše napredne persistentne pretnje (APT) sa dobrim izviđanjem."],
                ["Koji su prioritetni digitalni tragovi za istragu CEO fraud napada?",
                 ["Samo finansijski zapisi bankovnih transakcija",
                  "Email headers, IP adrese, registracija domena, deepfake metadata i telefonski CDR zapisi",
                  "Samo log fajlovi email servera",
                  "CEO fraud ne ostavlja digitalne tragove"],
                 1, "Korelacija više izvora podataka daje kompletniju sliku napadačke infrastrukture i TTP-ova."]
            ],
            "PR tim": [
                ["Da li javno saopštiti o pokušaju CEO frauda dok je istraga u toku?",
                 ["Da odmah — potpuna transparentnost gradi poverenje",
                  "Ne pre koordinacije sa pravnim timom i istragom — curenje informacija može upozoriti napadače",
                  "Nikad — ovakve informacije ne treba deli sa javnošću",
                  "Samo ako novinar već ima informaciju"],
                 1, "Preuranjena objava može upozoriti napadače i otežati istragu — koordinacija je ključna."],
                ["Zaposleni su uplašeni i šire glasine o bankrotu kompanije. Šta PR tim radi?",
                 ["Ignorisati interne glasine — fokus je na eksternoj komunikaciji",
                  "Internu komunikaciju tretirati jednako ozbiljno kao eksternu — brzo, jasno i iskreno informisati zaposlene",
                  "Zabraniti zaposlenima da govore o incidentu pod pretnjom otkaza",
                  "Prepustiti HR-u da se bavi internom komunikacijom"],
                 1, "Zaposleni su i glasnici i novinari — uninformisani zaposleni su izvor nekontrolisanog curenja informacija."],
                ["Kada i kako saopštiti da je pokušaj CEO frauda sprečen?",
                 ["Ne saopštavati nikad — to je slabost koja otkriva da smo bili meta",
                  "Saopštiti po završetku istrage kao primer uspešnog odgovora — gradi poverenje i edukovje",
                  "Odmah, bez koordinacije sa istragom",
                  "Samo akcionarima i investitorima, ne javnosti"],
                 1, "Uspešno odbijen napad, transparentno saopšten, može postati primer dobre prakse koji gradi reputaciju."]
            ],
            "Policija/Tužilaštvo": [
                ["Koja je nadležnost u istrazi CEO frauda koji uključuje strane račune?",
                 ["Samo lokalna policija — strana jurisdikcija blokira istragu",
                  "Kombinacija lokalne istrage i Interpol crvenog obaveštenja uz aktivaciju MLA procedure",
                  "Jedino SEC (američka regulatorna agencija) ima nadležnost",
                  "CEO fraud nije krivično delo jer je pokušaj, a ne izvršeno delo"],
                 1, "Međunarodna finansijska istraga zahteva kombinaciju Interpola, MLA i direktnih kontakata sa stranim policijama."],
                ["Fizički upad u server room — koja krivična dela se istražuju?",
                 ["Samo neovlašćen pristup zaštićenom objektu",
                  "Neovlašćen pristup, ometanje rada informacionih sistema, potencijalna špijunaža",
                  "Samo krađa ako je hardver uzet",
                  "Fizički upad nije krivično delo ako server room nije obezbeđen"],
                 1, "Fizički upad u IKT infrastrukturu može biti kvalifikovan i kao špijunaža ako se dokaže veza sa stranim obaveštajnim službama."],
                ["Kako legalno pratiti sumnjive osobe identifikovane pri pokušaju upada?",
                 ["Privatna detektivska agencija je brža i efikasnija od policijeove procedure",
                  "Sudski nalog za praćenje i tehničko snimanje po ZKP-u uz koordinaciju sa operativnim timom",
                  "Bez naloga — hitnost situacije opravdava trenutno praćenje",
                  "Praćenje nije dozvoljeno u demokratskim sistemima"],
                 1, "ZKP propisuje proceduru hitnih mera — sudski nalog se može dobiti u roku od 24h u hitnim slučajevima."]
            ],
            "Diplomatija": [
                ["Napad uključuje pokušaj fizičke infiltracije — menja li to diplomatsku procenu?",
                 ["Ne, fizička i sajber bezbednost su odvojene sfere diplomatije",
                  "Da, koordinacija fizičkih i sajber napada sugeriše sofisticiranog aktera sa potencijalom državne podrške",
                  "Fizički napadi su uvek delo lokalne kriminalne grupe bez veze sa stranim akterima",
                  "Diplomatija ne analizira tehničke aspekte napada"],
                 1, "Koordinovani hibridni napadi su zaštitni znak naprednih persistentnih pretnji (APT) koje najčešće imaju državno sponzorstvo."],
                ["Koja je diplomatska implikacija ako se utvrdi da je nalog za prevaru dat iz inostranstva?",
                 ["Automatski opravdava odmazdu",
                  "Aktivira diplomatske konsultacije sa saveznicima i razmatranje srazmernog odgovora kroz etablirane kanale",
                  "Nema diplomatskih implikacija — finansijska prevara je isključivo krivičnopravno pitanje",
                  "Zahteva hitno sazvanje Saveta bezbednosti UN"],
                 1, "Proporcionalnost i koordinacija sa saveznicima su ključni principi diplomatskog odgovora na sajber napade."],
                ["Koje multilateralne institucije mogu pomoći u finansijskoj istrazi transnacionalnog sajber napada?",
                 ["Samo NATO — jedina relevantna multilateralna organizacija",
                  "Interpol, Egmont Group, FATF i bilateralni MLA sporazumi",
                  "EU može da nametne sankcije odmah bez istrage",
                  "Multilateralne institucije nemaju nadležnost u sajber finansijskim istrasgama"],
                 1, "Egmont Group i FATF koordinišu međunarodnu finansijsku istragu — Interpol operativno podržava policijsku saradnju."]
            ]
        }
    },
    {
        "title": "FAZA 3 — Eksfiltracija podataka i obaveze obaveštavanja",
        "clock": "09:45",
        "narrative": "Na Telegram kanalu GRID_LEAKS objavljen je uzorak podataka: ime, adresa, broj mernog mesta i iznos računa. Poverenik za informacije od javnog značaja traži hitne informacije.",
        "visual": "CHANNEL: @GRID_LEAKS\n━━━━━━━━━━━━━━━━━━━━━━━\nBlackout is just the beginning.\n\nWe have customer records:\n→ Full names & addresses\n→ Meter IDs & billing history\n→ Payment status & debt records\n\nSample (100 records) attached.\nFull release in 6 hours unless payment.\n━━━━━━━━━━━━━━━━━━━━━━━",
        "inject": "Stiže dopis Poverenika: tražen opis incidenta, kategorije podataka, broj lica i mere ublažavanja.",
        "questions": {
            "shared": ["Koji je zakonski rok za prijavu povrede podataka o ličnosti Povereniku po ZZPL?",
                ["30 dana od otkrivanja povrede podataka",
                 "72 sata od saznanja za povredu podataka, bez nepotrebnog odlaganja",
                 "Rok ne postoji — prijava se vrši po okončanju istrage",
                 "Rok je 24 sata ali samo za zdravstvene i finansijske podatke"],
                1, "ZZPL čl. 53 propisuje rok od 72 sata za prijavu Povereniku — kašnjenje je samo po sebi prekršaj."],
            "Izvršni bord direktora": [
                ["Koji nivo transparentnosti prema javnosti je ispravan kada su podaci objavljeni na Telegramu?",
                 ["Potpuna tišina — svaka informacija narušava reputaciju",
                  "Kontrolisana transparentnost — potvrđene informacije uz savete građanima bez spekulacija",
                  "Objaviti odmah sve tehničke detalje",
                  "Prebaciti svu komunikaciju na državne organe"],
                 1, "Kontrolisana transparentnost gradi poverenje — tišina se tumači kao prikrivanje."],
                ["Koji je finansijski i reputacioni rizik od potencijalnih tužbi pogođenih građana?",
                 ["Minimalan — kompanija nije kriva za napad",
                  "Značajan — EDG može biti odgovoran za nedovoljnu zaštitu podataka",
                  "Rizik postoji jedino ako pogođeni građani podnesu kolektivnu tužbu",
                  "Rizik postoji samo ako su objavljeni podaci doveli do direktne finansijske štete"],
                 1, "Odgovornost rukovatelja podataka je objektivna — nedovoljna zaštita je osnov odgovornosti."],
                ["EDG razmatra angažovanje eksternog kriznog PR tima. Kada je pravi momenat?",
                 ["Posle okončanja istrage kada se zna puna slika",
                  "Odmah — eksterna ekspertiza za krizne situacije treba biti angažovana što pre",
                  "Samo ako interni tim ne može da izađe na kraj sa situacijom",
                  "Eksterna podrška nije potrebna — interni timovi uvek bolje razumeju situaciju"],
                 1, "Brzina reakcije u kriznim komunikacijama je kritična — čekanje na 'pravi momenat' znači propuštanje ključnih 72 sata."]
            ],
            "Pravni tim": [
                ["Koji podaci su obuhvaćeni obavezom prijave Povereniku po objavi uzorka na Telegramu?",
                 ["Samo finansijski podaci kao najosetljivija kategorija",
                  "Svi lični podaci čija je bezbednost narušena: ime, adresa, broj mernog mesta, iznos računa",
                  "Samo podaci koji nisu prethodno bili javno dostupni",
                  "Prijava nije potrebna jer je podatke objavio napadač, a ne EDG"],
                 1, "ZZPL ne pravi razliku po tome ko je objavio podatke — svaka neovlašćena obrada je povreda."],
                ["Da li EDG mora direktno obavestiti pogođene građane o curenju njihovih podataka?",
                 ["Ne, dovoljno je obavestiti Poverenika",
                  "Da, ukoliko povreda može prouzrokovati visok rizik za prava i slobode lica",
                  "Samo one koji su se već žalili ili kontaktirali korisničku podršku",
                  "Obaveštavanje građana je isključivo ovlašćenje Poverenika"],
                 1, "ZZPL čl. 54 propisuje direktno obaveštavanje pogođenih lica kada postoji visok rizik."],
                ["Koja je sankcija za neblagovremenu prijavu povrede podataka Povereniku?",
                 ["Opomena bez finansijske sankcije za prvi prekršaj",
                  "Novčana kazna do 2% globalnog godišnjeg prometa ili do 2M EUR za pravno lice",
                  "Samo pokretanje upravnog postupka",
                  "Sankcija nije propisana — ZZPL nema kaznene odredbe"],
                 1, "ZZPL predviđa significantne kazne — neblagovremenost je samostalan prekršaj, odvojen od same povrede."]
            ],
            "IT/CERT tim": [
                ["Kako forenzički utvrditi da li su objavljeni podaci zaista iz EDG baza podataka?",
                 ["Verujemo napadačima na reč",
                  "Verifikacija uzorka poređenjem sa internim bazama uz forenzičku analizu vektora eksfiltracije",
                  "Proglasiti sve lažnim — teret dokaza je na napadaču",
                  "To nije u nadležnosti IT tima"],
                 1, "Verifikacija autentičnosti je preduslov za pravovremenu prijavu — lažne alarme i realne incidente treba razlikovati forenzički."],
                ["Koji forenzički artefakti su ključni za utvrđivanje vektora eksfiltracije podataka?",
                 ["Isključivo antivirusni logovi",
                  "Mrežni flow logovi, DLP upozorenja, pristupni logovi baze podataka, endpoint i proxy logovi",
                  "Email arhiva kompanijskog servera",
                  "Fizički pregled serverske sobe"],
                 1, "Eksfiltracija ostavlja tragove na više nivoa — korelacija različitih izvora logova jedina daje kompletnu sliku."],
                ["Kako bezbedno prijaviti tehnički nalaz o eksfiltraciji Povereniku bez odavanja osetljivih detalja?",
                 ["Poslati kompletan tehnički izveštaj sa svim detaljima",
                  "Strukturiran izveštaj: kategorije podataka, procenjeni broj lica, mere ublažavanja — bez TTP detalja koji bi upozorili napadača",
                  "Prijaviti samo ako je eksfiltracija 100% potvrđena",
                  "Tehnički izveštaj šalje isključivo tužilaštvo, ne Poverenik"],
                 1, "Izveštaj Povereniku mora biti informativan ali ne sme otkriti operativne detalje koji bi upozorili napadača ili oštetili istragu."]
            ],
            "PR tim": [
                ["Građani postavljaju pitanja na društvenim mrežama o bezbednosti svojih podataka. Šta PR tim radi?",
                 ["Ignorisati — individualni komentari nisu medijska kriza",
                  "Pripremiti i objaviti FAQ sa jasnim odgovorima i savetima građanima kako da zaštite sebe",
                  "Blokirati negativne komentare i kritičare",
                  "Odgovoriti samo na komentare sa verifikovanim nalozima"],
                 1, "Proaktivan FAQ smanjuje pritisak na kanale korisničke podrške i pokazuje odgovornost."],
                ["Mediji traže listu konkretnih žrtava čiji su podaci procureli. Odgovor PR tima?",
                 ["Dostaviti listu da se pokaže transparentnost",
                  "Odbiti pozivajući se na zaštitu privatnosti i navesti da su pogođeni direktno obavešteni",
                  "Reći da lista ne postoji",
                  "Prepustiti Povereniku da komunicira sa medijima"],
                 1, "Objavljivanje liste žrtava dodatno krši ZZPL — odbijanje uz objašnjenje je pravno i etički jedino ispravno."],
                ["Kako komunicirati sa pogođenim korisnicima direktno?",
                 ["Javnim saopštenjem na web sajtu — to je dovoljno",
                  "Direktnim pismom/email-om sa jasnim opisom šta se desilo, koji podaci su ugroženi i šta korisnik treba da uradi",
                  "Putem call centra — samo oni koji zovu dobijaju informaciju",
                  "Komunikacija sa korisnicima nije obaveza EDG-a po ZZPL-u"],
                 1, "Direktna komunikacija je zakonska obaveza i etička norma — javno saopštenje nije zamena za personalizovano obaveštenje."]
            ],
            "Policija/Tužilaštvo": [
                ["Telegram kanal objavljuje ukradene podatke. Kako bezbedno akvizovati ovaj sadržaj kao dokaz?",
                 ["Screenshot je dovoljan dokaz",
                  "Forenzička akvizicija sadržaja kanala uz dokumentovanu metodologiju, hash verifikaciju i chain of custody",
                  "Kontaktirati Telegram direktno i tražiti otkrivanje identiteta admina",
                  "OSINT istraživanje bez dokumentacije je brže i efikasnije"],
                 1, "Forenzički integritet digitalnih dokaza zahteva dokumentovanu metodologiju koja može biti proverena na suđenju."],
                ["Da li Telegram kao platforma mora dostaviti podatke o korisniku na zahtev srpske policije?",
                 ["Da, automatski — svi tech giganti moraju poštovati srpsko pravo",
                  "Zavisi od MLA sporazuma i procene Telegrama — procedura traje ali postoji",
                  "Ne — Telegram nikad ne sarađuje sa policijom",
                  "Samo uz nalog Međunarodnog suda pravde"],
                 1, "Telegram ima politiku ograničene saradnje sa vlastima — MLA procedura je formalni mehanizam ali sa neizvesnim ishodom."],
                ["Koje istražne radnje se preduzimaju za utvrđivanje identiteta napadača na osnovu Telegram kanala?",
                 ["Samo praćenje Telegram kanala",
                  "OSINT analiza kanala, blockchain analiza kripto novčanika, MLA zahtev, i analiza TTP preklapanja sa poznatim grupama",
                  "Hapšenje svih admin na sličnim kanalima",
                  "Istraga je nemoguća bez fizičke identifikacije napadača"],
                 1, "Višeslojna istraga kombinuje tehničke, pravne i obaveštajne metode za identifikaciju napadača."]
            ],
            "Diplomatija": [
                ["Telegram kanal koristi srpski jezik i referencira lokalne aktere. Šta to diplomatski znači?",
                 ["Potvrđuje da je napadač domaći akter — nema diplomatske implikacije",
                  "Ništa konkluzivno — napadači namerno pišu na jeziku žrtve da otežaju atribuciju ili da regrutuju lokalne simpatizere",
                  "Automatski isključuje stranu državnu umešanost",
                  "Diplomatija ne analizira jezičke aspekte napada"],
                 1, "Jezik i kulturne reference se koristite kao taktika maskiranja — ne mogu biti jedini osnov za zaključak o poreklu."],
                ["Koja je diplomatska uloga u komunikaciji sa Poverenikom i međunarodnim telima?",
                 ["Diplomatija nema ulogu — Poverenik je domaće telo",
                  "Koordinacija sa međunarodnim partnerima koji imaju slične incidente i informisanje EU institucija",
                  "Blokirati svaku komunikaciju Poverenika sa stranim telima",
                  "Preuzeti svu komunikaciju Poverenika sa EU institucijama"],
                 1, "EU institucije (EDPB, ENISA) prate značajne incidente — proaktivna komunikacija bolja je od reaktivnog odgovaranja."],
                ["Šest zemalja je imalo slične incidente u poslednjih mesec dana. Diplomatska akcija?",
                 ["Čekati da svaka zemlja samostalno reaguje",
                  "Inicirati multilateralnu koordinaciju kroz OSCE ili bilateralne kanale za razmenu informacija o napadačkoj infrastrukturi",
                  "Optužiti jednu od tih zemalja za koordinaciju napada",
                  "Nema diplomatske akcije jer su to odvojeni incidenti"],
                 1, "Serijski napadi ukazuju na istu napadačku grupu — multilateralna razmena informacija ubrzava atribuciju i zajednički odgovor."]
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
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

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
