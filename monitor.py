#!/usr/bin/env python3
"""
Aanbestedingsmonitor - notificatiesysteem voor openbare aanbestedingen
Bronnen: TED (EU), TenderNed (NL), BOAMP (FR), Portail des marchés publics (LU).
België wordt gedekt via TED (Europese drempels); zie README voor nationale BE-publicaties.

Alleen Python-standaardbibliotheek (3.9+). Gebruik:
    python3 monitor.py                 # normale run (sinds laatste run)
    python3 monitor.py --dagen 14      # forceer terugkijkperiode
    python3 monitor.py --geen-melding  # zonder macOS-melding
    python3 monitor.py --alleen ted    # één bron testen
"""
import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASIS = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASIS, "data")
OUT_DIR = os.path.join(BASIS, "output")
LOG_DIR = os.path.join(BASIS, "logs")
UA = "Aanbestedingsmonitor/1.0 (+publieke open data; contact via gebruiker)"

log = logging.getLogger("monitor")

# ---------------------------------------------------------------- HTTP


def _curl(url, data=None, headers=None, timeout=60):
    cmd = ["/usr/bin/curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd += ["-w", "\n%{http_code}", url]
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 10)
    if p.returncode != 0:
        raise IOError("curl fout %s: %s" % (p.returncode, p.stderr.decode("utf-8", "replace")[:200]))
    body, _, code = p.stdout.rpartition(b"\n")
    if not code.strip().startswith(b"2"):
        raise IOError("HTTP %s voor %s: %s" % (code.decode(), url[:120], body[:300].decode("utf-8", "replace")))
    return body


_gebruik_curl = False


def http(url, data=None, headers=None, timeout=60, pogingen=3):
    """GET/POST met retries. Valt terug op /usr/bin/curl bij SSL-problemen (macOS Python)."""
    global _gebruik_curl
    headers = dict(headers or {})
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    laatste = None
    for poging in range(pogingen):
        try:
            if _gebruik_curl and os.path.exists("/usr/bin/curl"):
                return _curl(url, data, headers, timeout)
            req = urllib.request.Request(url, data=data, headers=dict(headers, **{"User-Agent": UA}))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            if e.code in (400, 401, 403, 404):
                raise IOError("HTTP %s voor %s: %s" % (e.code, url[:120], body))
            laatste = IOError("HTTP %s: %s" % (e.code, body))
        except urllib.error.URLError as e:
            if "CERTIFICATE" in str(e).upper() and os.path.exists("/usr/bin/curl"):
                log.info("SSL-certificaten van Python niet bruikbaar; overschakelen op curl")
                _gebruik_curl = True
                continue
            laatste = e
        except Exception as e:  # noqa
            laatste = e
        time.sleep(2 * (poging + 1))
    raise IOError("Mislukt na %d pogingen: %s (%s)" % (pogingen, url[:120], laatste))


def http_json(url, data=None, headers=None, timeout=60):
    return json.loads(http(url, data, headers, timeout).decode("utf-8"))


# ---------------------------------------------------------------- helpers


def vouw(s):
    """lowercase + accenten weg, voor matching."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def kies_taal(v, voorkeur=("nld", "fra", "eng", "deu")):
    """TED-meertalige velden: {'fra': ['..']} -> string."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(kies_taal(x) for x in v[:1])
    if isinstance(v, dict):
        for t in voorkeur:
            if t in v and v[t]:
                x = v[t]
                return x[0] if isinstance(x, list) else str(x)
        for x in v.values():
            if x:
                return x[0] if isinstance(x, list) else str(x)
    return str(v)


def lijst(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def datum(s):
    """Neemt de eerste YYYY-MM-DD uit een string."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s or "")
    return m.group(0) if m else ""


def normaliseer_cpv(c):
    m = re.match(r"\s*(\d{8})", str(c))
    return m.group(1) if m else None


def dedup_sleutel(r):
    t = re.sub(r"[^a-z0-9]+", " ", vouw(r.get("titel", ""))).strip()[:70]
    b = re.sub(r"[^a-z0-9]+", " ", vouw(r.get("koper", ""))).strip()[:25]
    return t + "|" + b


FRAMEWORK_RE = re.compile(r"raamovereenkomst|raamcontract|accord[s]?[- ]cadre|framework agreement|dynamisch aankoopsysteem|"
                          r"acquisition dynamique|dynamic purchasing", re.I)


def record(**kw):
    basis = dict(id="", bron="", land="", titel="", beschrijving="", koper="", gepubliceerd="", deadline="",
                 cpv=[], aard="", soort="", raamovereenkomst=None, niet_gegund=False, reden_niet_gegund="",
                 reden_code="", waarde=None, munt="", url="", procedure="", aantal_offertes=None)
    basis.update(kw)
    basis["cpv"] = sorted(set(c for c in (normaliseer_cpv(x) for x in basis["cpv"]) if c))
    return basis


# ---------------------------------------------------------------- bron: TED

TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_VELDEN = ["publication-number", "notice-title", "buyer-name", "buyer-country", "classification-cpv",
              "publication-date", "notice-type", "contract-nature", "deadline-receipt-tender-date-lot", "links",
              "description-lot", "framework-agreement-lot", "estimated-value-lot", "estimated-value-cur-lot",
              "procedure-type", "winner-selection-status", "non-award-justification", "received-submissions-type-val"]
REDEN_LABEL = {
    "no-rece": "Geen offertes of aanvragen ontvangen",
    "all-rej": "Alle offertes/aanvragen afgewezen",
    "chan-need": "Gewijzigde behoefte van de aanbesteder",
    "ins-funds": "Onvoldoende middelen",
    "no-awa": "Niet gegund (andere reden)",
    "one-admis": "Slechts één aanvaardbare offerte",
}


def ted_soort(nt):
    nt = nt or ""
    if nt.startswith("can-") or nt in ("veat",):
        return "resultaat"
    if nt.startswith("cn-") or nt in ("pin-cfc-standard", "pin-cfc-social", "pin-rtl", "subco", "qu-sy"):
        return "oproep"
    if nt.startswith("pin"):
        return "vooraankondiging"
    if nt.startswith("corr"):
        return "rectificatie"
    return "overig"


def ted_titel(t):
    """TED-titels zijn 'Land – CPV-omschrijving – Titel'; houd alleen de titel."""
    delen = t.split(" – ")
    return " – ".join(delen[2:]) if len(delen) >= 3 else t


def ted_naar_record(n):
    pub = n.get("publication-number", "")
    links = n.get("links") or {}
    html_links = links.get("html") or {}
    url = html_links.get("NLD") or html_links.get("FRA") or html_links.get("ENG") or \
        "https://ted.europa.eu/nl/notice/-/detail/%s" % pub
    nat = lijst(n.get("contract-nature"))
    fa = lijst(n.get("framework-agreement-lot"))
    nonaw = [x for x in lijst(n.get("non-award-justification")) if x]
    wss = lijst(n.get("winner-selection-status"))
    soort = ted_soort(n.get("notice-type"))
    niet = False
    reden_code = ""
    if soort == "resultaat":
        if nonaw or any(str(w).startswith("clos-nw") for w in wss):
            niet = True
            for code in ("no-rece", "all-rej"):
                if code in nonaw:
                    reden_code = code
                    break
            else:
                reden_code = nonaw[0] if nonaw else "no-awa"
    # Bij meerdere percelen: geef aan dat het (deels) niet gegund is
    deels = niet and any(str(w).startswith("selec") for w in wss)
    reden = REDEN_LABEL.get(reden_code, "Niet gegund (code %s)" % reden_code)
    if deels:
        reden += " (enkele percelen)"
    waarden = [float(x) for x in lijst(n.get("estimated-value-lot")) if re.match(r"^\d+(\.\d+)?$", str(x))]
    offertes = [int(x) for x in lijst(n.get("received-submissions-type-val")) if str(x).isdigit()]
    return record(
        id="ted:" + pub, bron="TED", land=(lijst(n.get("buyer-country")) or [""])[0],
        titel=ted_titel(kies_taal(n.get("notice-title"))), beschrijving=kies_taal(n.get("description-lot"))[:1500],
        koper=kies_taal(n.get("buyer-name")), gepubliceerd=datum(n.get("publication-date")),
        deadline=min([datum(x) for x in lijst(n.get("deadline-receipt-tender-date-lot")) if datum(x)] or [""]),
        cpv=lijst(n.get("classification-cpv")), aard=nat[0] if nat else "", soort=soort,
        raamovereenkomst=(any(x and x != "none" for x in fa) if fa else None),
        niet_gegund=niet, reden_code=reden_code, reden_niet_gegund=reden if niet else "",
        waarde=sum(waarden) if waarden else None, munt=(lijst(n.get("estimated-value-cur-lot")) or [""])[0],
        url=url, procedure=n.get("procedure-type") or "",
        aantal_offertes=(min(offertes) if (offertes and niet) else None),
    )


def haal_ted(cfg, van, tot):
    landen = " ".join(cfg["landen"])
    q = "publication-date>=%s AND publication-date<=%s AND buyer-country IN (%s)" % (
        van.strftime("%Y%m%d"), tot.strftime("%Y%m%d"), landen)
    uit, pagina, limiet = [], 1, 250
    while True:
        body = {"query": q, "fields": TED_VELDEN, "limit": limiet, "page": pagina, "paginationMode": "PAGE_NUMBER"}
        j = http_json(TED_URL, body, timeout=120)
        notices = j.get("notices") or []
        uit += [ted_naar_record(n) for n in notices]
        totaal = j.get("totalNoticeCount") or 0
        log.info("TED pagina %d: %d (totaal %d)", pagina, len(notices), totaal)
        if not notices or pagina * limiet >= totaal or pagina >= 60:
            break
        pagina += 1
        time.sleep(0.5)
    return uit


# ---------------------------------------------------------------- bron: TenderNed

TN = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties"
TN_SOORT = {"AAO": "oproep", "AGO": "resultaat", "VBE": "resultaat", "REC": "rectificatie", "VOP": "vooraankondiging",
            "MAC": "oproep", "WNO": "oproep", "RVO": "oproep"}
TN_AARD = {"L": "supplies", "D": "services", "W": "works"}
NIET_GEGUND_NL = re.compile(r"niet gegund|geen (geldige |passende |aanvaardbare )?(inschrijving|offerte|aanmelding)|"
                            r"geen inschrijvers|ingetrokken|zonder gunning|niet tot gunning", re.I)


def haal_tenderned(cfg, van, tot, bekend):
    uit, pagina = [], 0
    while True:
        url = "%s?page=%d&size=100&publicatieDatumVanaf=%s&publicatieDatumTot=%s" % (
            TN, pagina, van.isoformat(), tot.isoformat())
        j = http_json(url)
        inhoud = j.get("content") or []
        for p in inhoud:
            pid = str(p.get("publicatieId"))
            rid = "tenderned:" + pid
            code = (p.get("typePublicatie") or {}).get("code", "")
            detail = {}
            if rid not in bekend:
                try:
                    detail = http_json("%s/%s" % (TN, pid))
                    time.sleep(0.15)
                except Exception as e:  # noqa
                    log.warning("TenderNed detail %s: %s", pid, e)
            tekst = (p.get("aanbestedingNaam") or "") + " " + (p.get("opdrachtBeschrijving") or "")
            soort = TN_SOORT.get(code, "overig")
            niet = False
            reden = ""
            if code == "VBE" and detail.get("isGegund") is False:
                niet, reden = True, "Vroegtijdig beëindigd zonder gunning"
            elif soort == "resultaat" and NIET_GEGUND_NL.search(tekst):
                niet, reden = True, "Niet gegund (volgens publicatietekst)"
            if niet and re.search(r"geen (geldige )?(inschrijving|offerte|aanmelding)|geen inschrijvers", tekst, re.I):
                reden = "Geen inschrijvingen ontvangen"
            uit.append(record(
                id=rid, bron="TenderNed", land="NLD", titel=p.get("aanbestedingNaam") or "",
                beschrijving=(p.get("opdrachtBeschrijving") or "")[:1500], koper=p.get("opdrachtgeverNaam") or "",
                gepubliceerd=datum(p.get("publicatieDatum")), deadline=datum(p.get("sluitingsDatum")),
                cpv=[c.get("code") for c in detail.get("cpvCodes") or []],
                aard=TN_AARD.get((p.get("typeOpdracht") or {}).get("code", ""), ""), soort=soort,
                raamovereenkomst=bool(FRAMEWORK_RE.search(tekst)), niet_gegund=niet,
                reden_niet_gegund=reden, reden_code="no-rece" if reden.startswith("Geen") else ("other" if niet else ""),
                url=(p.get("link") or {}).get("href") or "https://www.tenderned.nl/aankondigingen/overzicht/" + pid,
                procedure=(p.get("procedure") or {}).get("omschrijving", ""),
            ))
        log.info("TenderNed pagina %d: %d (totaal %s)", pagina, len(inhoud), j.get("totalElements"))
        if j.get("last", True) or not inhoud or pagina > 50:
            break
        pagina += 1
    return uit


# ---------------------------------------------------------------- bron: BOAMP

BOAMP = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/exports/json"
BOAMP_SOORT = {"APPEL_OFFRE": "oproep", "ATTRIBUTION": "resultaat", "ANNULATION": "resultaat",
               "RECTIFICATIF": "rectificatie", "PRE-INFORMATION": "vooraankondiging"}
BOAMP_AARD = {"FOURNITURES": "supplies", "SERVICES": "services", "TRAVAUX": "works"}
CPV_IN_DONNEES = re.compile(r"(?:ItemClassificationCode|CPV|cpv)[^0-9]{0,80}?(\d{8})")


def haal_boamp(cfg, van, tot):
    where = "dateparution>=date'%s' AND dateparution<=date'%s'" % (van.isoformat(), tot.isoformat())
    select = "idweb,objet,nomacheteur,dateparution,datelimitereponse,nature,type_marche,url_avis,famille,donnees,procedure_libelle,etat"
    url = BOAMP + "?" + urllib.parse.urlencode({"where": where, "select": select})
    rijen = http_json(url, timeout=300)
    uit = []
    for r in rijen:
        if r.get("famille") == "JOUE":
            continue  # Europese publicaties komen gestructureerd via TED
        donnees = r.get("donnees") or ""
        objet = r.get("objet") or ""
        nature = r.get("nature") or ""
        soort = BOAMP_SOORT.get(nature, "overig")
        dv = vouw(donnees)
        niet, reden, code = False, "", ""
        if nature == "ANNULATION":
            niet, reden, code = True, "Avis d'annulation (procedure stopgezet)", "other"
        elif nature == "ATTRIBUTION":
            if "no-rece" in dv or "aucune offre" in dv or "aucune candidature" in dv:
                niet, reden, code = True, "Geen offertes ontvangen (infructueux)", "no-rece"
            elif "all-rej" in dv or "infructueu" in dv:
                niet, reden, code = True, "Infructueux: geen aanvaardbare offertes", "all-rej"
            elif "sans suite" in dv:
                niet, reden, code = True, "Zonder gevolg geklasseerd (sans suite)", "other"
        tm = lijst(r.get("type_marche"))
        uit.append(record(
            id="boamp:" + str(r.get("idweb")), bron="BOAMP", land="FRA", titel=objet,
            beschrijving="", koper=r.get("nomacheteur") or "", gepubliceerd=datum(r.get("dateparution")),
            deadline=datum(r.get("datelimitereponse")), cpv=CPV_IN_DONNEES.findall(donnees)[:20],
            aard=BOAMP_AARD.get(tm[0], "") if tm else "", soort=soort,
            raamovereenkomst=bool(FRAMEWORK_RE.search(objet) or "fa-wo-rc" in dv or "fa-w-rc" in dv or "accord-cadre" in dv),
            niet_gegund=niet, reden_niet_gegund=reden, reden_code=code,
            url=r.get("url_avis") or "", procedure=r.get("procedure_libelle") or "",
        ))
    log.info("BOAMP: %d rijen, %d na weglaten JOUE-dubbels", len(rijen), len(uit))
    return uit


# ---------------------------------------------------------------- bron: Luxemburg (pmp.lu)

PMP = "https://pmp.b2g.etat.lu/"
MAANDEN = [("janv", 1), ("fevr", 2), ("mars", 3), ("avr", 4), ("mai", 5), ("juin", 6), ("juil", 7), ("aout", 8),
           ("sept", 9), ("oct", 10), ("nov", 11), ("dec", 12)]


def _tekst(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _pmp_datum(blok):
    m = re.search(r'class="day\s*">\s*<span>(\d+)</span>.*?class="month\s*">\s*<span>([^<]+)</span>.*?class="year\s*">\s*<span>(\d{4})',
                  blok, re.S)
    if not m:
        return ""
    naam = vouw(m.group(2)).strip(" .")
    mnd = next((v for k, v in MAANDEN if naam.startswith(k) or k.startswith(naam[:4]) and len(naam) >= 3), None)
    try:
        return dt.date(int(m.group(3)), mnd, int(m.group(1))).isoformat()
    except Exception:  # noqa
        return ""


def haal_pmp_lu(cfg, van, tot):
    """Best effort: zoekt per trefwoord op het Luxemburgse portaal (eerste resultatenpagina)."""
    termen = []
    for p in cfg["profielen"]:
        if "oproep" in p.get("soort", []):
            termen += [t for t in p.get("trefwoorden", []) if len(t) > 3 and "*" not in t
                       and not re.search(r"[A-Z]{2}", t)][:6]
    termen = list(dict.fromkeys(termen))
    gezien, uit = set(), []
    for t in termen:
        url = PMP + "?" + urllib.parse.urlencode({"page": "Entreprise.EntrepriseAdvancedSearch", "AllCons": "", "keyWord": t})
        try:
            pagina = http(url, timeout=60).decode("utf-8", "replace")
        except Exception as e:  # noqa
            log.warning("pmp.lu '%s': %s", t, e)
            continue
        blokken = re.split(r'<div id="row-consultation-', pagina)[1:]
        for b in blokken:
            mref = re.search(r'_refCons" value="(\d+)"', b)
            morg = re.search(r'_orgCons" value="([^"]+)"', b)
            if not mref or mref.group(1) in gezien:
                continue
            gezien.add(mref.group(1))
            titel = re.search(r'truncate">\s*<span[^>]*>(.*?)</span>', b, re.S)
            objet = re.search(r"Objet : </strong>.*?<span>(.*?)</span>", b, re.S)
            org = re.search(r"Organisme : </strong>\s*</span>\s*<span class=\"small\">(.*?)</span>", b, re.S)
            cat = re.search(r'cons_categorie">\s*<span>(.*?)</span>', b, re.S)
            refnr = re.search(r'<div class="small pull-left">\s*([^<]+?)\s*</div>', b)
            delen = b.split("cons_dateEnd")
            aard = {"fournitures": "supplies", "services": "services", "travaux": "works"}.get(
                vouw(_tekst(cat.group(1))) if cat else "", "")
            uit.append(record(
                id="pmp:" + mref.group(1), bron="PMP Luxembourg", land="LUX",
                titel=_tekst(titel.group(1)) if titel else (refnr.group(1) if refnr else ""),
                beschrijving=_tekst(objet.group(1))[:1500] if objet else "",
                koper=_tekst(org.group(1)).replace("( - )", "").strip() if org else "",
                gepubliceerd=_pmp_datum(delen[0]), deadline=_pmp_datum(delen[1]) if len(delen) > 1 else "",
                aard=aard, soort="oproep",
                raamovereenkomst=bool(FRAMEWORK_RE.search(b)) or "SAD" in b[:2500],
                url="https://pmp.b2g.etat.lu/entreprise/consultation/%s?orgAcronyme=%s" % (
                    mref.group(1), morg.group(1) if morg else ""),
            ))
        time.sleep(0.4)
    # Alleen recente publicaties (rest is al bekend of oud)
    grens = (van - dt.timedelta(days=3)).isoformat()
    uit = [r for r in uit if not r["gepubliceerd"] or r["gepubliceerd"] >= grens]
    log.info("pmp.lu: %d recente consultaties via %d zoektermen", len(uit), len(termen))
    return uit


# ---------------------------------------------------------------- Coming up (aflopende contracten)

CU_VELDEN = ["publication-number", "notice-type", "notice-title", "buyer-name", "buyer-country", "classification-cpv",
             "publication-date", "contract-conclusion-date", "winner-decision-date", "duration-period-value-lot",
             "duration-period-unit-lot", "contract-duration-start-date-lot", "contract-duration-end-date-lot",
             "renewal-maximum-lot", "renewal-description-lot", "winner-name", "result-value-lot", "estimated-value-lot",
             "framework-agreement-lot", "description-lot", "links"]
EFORMS_START = "20231101"   # vanaf hier heeft TED gestructureerde looptijden
EENHEID_DAGEN = {"DAY": 1, "WEEK": 7, "MONTH": 30.44, "YEAR": 365.25}

VERLENG_RE = [
    (re.compile(r"(\d+)\s*(?:fois|x|keer|maal|times)\s*(?:(?:met|de|par|of|van|with|by)\s+)?(?:(?:période|periode|period)\s*(?:d'|de|van|of)?\s*)?(?:(?:une?|een|one)\s+)?(\d+)?\s*(an|ans|année|années|jaar|jaren|year|years|mois|maand|maanden|months?)\b", re.I), "n_x_p"),
    (re.compile(r"(?:durée|looptijd|duration)\s*(?:maximale|maximum|max\.?|totale)[^0-9]{0,40}(\d+)\s*(an|ans|jaar|jaren|years?|mois|maanden|months?)", re.I), "max"),
]


def _dagen(waarde, eenheid):
    try:
        return float(waarde) * EENHEID_DAGEN.get(str(eenheid).upper(), 30.44)
    except Exception:  # noqa
        return None


def _eenheid_tekst(e):
    e = vouw(e)
    return "YEAR" if e.startswith(("an", "jaar", "jaren", "year")) else "MONTH"


def verlenging_dagen(tekst, basis_dagen):
    """Probeert uit vrije tekst de extra looptijd door verlengingen te halen."""
    for rx, soort in VERLENG_RE:
        m = rx.search(tekst or "")
        if not m:
            continue
        if soort == "n_x_p":
            n = int(m.group(1))
            per = int(m.group(2)) if m.group(2) else 1
            return n * _dagen(per, _eenheid_tekst(m.group(3)))
        if soort == "max" and basis_dagen:
            totaal = _dagen(int(m.group(1)), _eenheid_tekst(m.group(2)))
            return max(0, totaal - basis_dagen)
    return None


def _plus(datum_s, dagen):
    d = dt.date.fromisoformat(datum_s)
    return (d + dt.timedelta(days=int(round(dagen)))).isoformat()


def cu_item(n, lijst_id, legacy=None):
    pub = n.get("publication-number", "")
    links = (n.get("links") or {}).get("html") or {}
    pubdatum = datum(n.get("publication-date"))
    soort = "contract"
    nt = n.get("notice-type") or ""
    if nt.startswith("pin"):
        soort = "vooraankondiging"
    titel = ted_titel(kies_taal(n.get("notice-title")))
    eind, eind_max, geschat, verlenging = "", "", False, None
    if legacy:
        titel = legacy.get("titel") or titel
        start = _plus(pubdatum, 120)  # gunning gemiddeld ±4 maanden na de aankondiging
        geschat = True
        if legacy.get("eind"):
            eind = legacy["eind"]
        elif legacy.get("duur_dagen"):
            eind = _plus(start, legacy["duur_dagen"])
        verlenging = legacy.get("verlenging")
        if eind and legacy.get("verleng_dagen"):
            eind_max = _plus(eind, legacy["verleng_dagen"])
    elif soort == "contract":
        einden = [datum(x) for x in lijst(n.get("contract-duration-end-date-lot")) if datum(x)]
        start = (min([datum(x) for x in lijst(n.get("contract-duration-start-date-lot")) if datum(x)] or [""])
                 or min([datum(x) for x in lijst(n.get("contract-conclusion-date")) if datum(x)] or [""])
                 or min([datum(x) for x in lijst(n.get("winner-decision-date")) if datum(x)] or [""]))
        if not start:
            start, geschat = pubdatum, True
        if einden:
            eind = max(einden)
        else:
            w, u = lijst(n.get("duration-period-value-lot")), lijst(n.get("duration-period-unit-lot"))
            duren = [_dagen(v, u[i] if i < len(u) else (u[0] if u else "MONTH")) for i, v in enumerate(w)]
            duren = [d for d in duren if d]
            if duren:
                eind = _plus(start, max(duren))
        ren = [int(x) for x in lijst(n.get("renewal-maximum-lot")) if str(x).isdigit()]
        if ren and max(ren) > 0:
            verlenging = "max. %d verlenging(en)" % max(ren)
            extra = verlenging_dagen(kies_taal(n.get("renewal-description-lot")) + " " + kies_taal(n.get("description-lot")), None)
            if eind:
                eind_max = _plus(eind, extra if extra else max(ren) * 365.25)
    waarden = [float(x) for x in lijst(n.get("result-value-lot")) or lijst(n.get("estimated-value-lot"))
               if re.match(r"^\d+(\.\d+)?$", str(x))]
    return {
        "id": "ted:" + pub, "lijst": lijst_id, "soort": soort, "bron": "TED",
        "land": (lijst(n.get("buyer-country")) or [""])[0], "titel": titel, "koper": kies_taal(n.get("buyer-name")),
        "cpv": sorted(set(c for c in (normaliseer_cpv(x) for x in lijst(n.get("classification-cpv"))) if c)),
        "gepubliceerd": pubdatum, "einddatum": eind, "einddatum_max": eind_max, "geschat": geschat,
        "verlenging": verlenging, "leverancier": ", ".join(dict.fromkeys(kies_taal(x) for x in lijst(n.get("winner-name"))[:3] if x))
        if isinstance(n.get("winner-name"), list) else kies_taal(n.get("winner-name")),
        "waarde": sum(waarden) if waarden else None,
        "url": links.get("NLD") or links.get("FRA") or "https://ted.europa.eu/nl/notice/-/detail/%s" % pub,
    }


def ted_alle(q, velden, max_pag=40):
    uit, pagina = [], 1
    while True:
        j = http_json(TED_URL, {"query": q, "fields": velden, "limit": 250, "page": pagina, "paginationMode": "PAGE_NUMBER"}, timeout=120)
        notices = j.get("notices") or []
        uit += notices
        if not notices or pagina * 250 >= (j.get("totalNoticeCount") or 0) or pagina >= max_pag:
            return uit
        pagina += 1
        time.sleep(0.4)


def legacy_xml(db, pub):
    row = db.execute("SELECT json FROM xmlcache WHERE pub=?", (pub,)).fetchone()
    if row:
        return json.loads(row[0])
    t = http("https://ted.europa.eu/en/notice/%s/xml" % pub, timeout=60).decode("utf-8", "replace")
    res = {}
    m = re.search(r"<OBJECT_CONTRACT>.*?<TITLE>\s*<P>(.*?)</P>", t, re.S)
    if m:
        res["titel"] = _tekst(m.group(1))
    einden = re.findall(r"<DATE_END>(\d{4}-\d{2}-\d{2})", t)
    if einden:
        res["eind"] = max(einden)
    duren = [_dagen(v, u) for u, v in re.findall(r'<DURATION TYPE="(\w+)">(\d+)</DURATION>', t)]
    duren = [d for d in duren if d]
    if duren:
        res["duur_dagen"] = max(duren)
    if "<RENEWAL/>" in t:
        descr = " ".join(_tekst(x) for x in re.findall(r"<RENEWAL_DESCR>(.*?)</RENEWAL_DESCR>", t, re.S))
        res["verlenging"] = descr[:160] or "verlenging mogelijk"
        res["verleng_dagen"] = verlenging_dagen(descr, res.get("duur_dagen"))
    db.execute("INSERT OR REPLACE INTO xmlcache VALUES(?,?)", (pub, json.dumps(res, ensure_ascii=False)))
    return res


def haal_coming_up(cfg, db):
    vandaag = dt.date.today()
    resultaat = []
    for lst in cfg.get("coming_up", []):
        maanden = lst.get("maanden", 12)
        grens = (vandaag + dt.timedelta(days=int(maanden * 30.44))).isoformat()
        basis = "buyer-country IN (%s) AND classification-cpv IN (%s)" % (" ".join(cfg["landen"]), " ".join(lst["cpv"]))
        items = []
        # 1. contracten gegund sinds eForms (gestructureerde looptijd)
        for n in ted_alle(basis + " AND notice-type IN (can-standard can-social can-desg) AND publication-date>=" + EFORMS_START, CU_VELDEN):
            items.append(cu_item(n, lst["id"]))
        # 2. oudere aankondigingen: looptijd uit de XML (eenmalig opgehaald, daarna uit cache)
        if lst.get("historiek_vanaf"):
            oud = ted_alle(basis + " AND notice-type IN (cn-standard cn-social) AND publication-date>=%s AND publication-date<%s"
                           % (lst["historiek_vanaf"], EFORMS_START), ["publication-number", "notice-type", "notice-title", "buyer-name",
                                                                     "buyer-country", "classification-cpv", "publication-date", "links"])
            nieuw_opgehaald = 0
            for n in oud:
                pub = n.get("publication-number")
                if not db.execute("SELECT 1 FROM xmlcache WHERE pub=?", (pub,)).fetchone():
                    if nieuw_opgehaald >= lst.get("xml_per_run", 700):
                        continue
                    nieuw_opgehaald += 1
                    time.sleep(0.2)
                try:
                    items.append(cu_item(n, lst["id"], legacy=legacy_xml(db, pub)))
                except Exception as e:  # noqa
                    log.warning("XML %s: %s", pub, e)
            db.commit()
            log.info("Coming up %s: %d oudere aankondigingen (%d XML nieuw opgehaald)", lst["id"], len(oud), nieuw_opgehaald)
        # 3. vooraankondigingen van het voorbije jaar
        vorig = (vandaag - dt.timedelta(days=365)).strftime("%Y%m%d")
        for n in ted_alle(basis + " AND notice-type IN (pin-only pin-buyer) AND publication-date>=" + vorig, CU_VELDEN, 8):
            items.append(cu_item(n, lst["id"]))
        # filter: loopt af (basis of met verlengingen) binnen het venster
        gekozen, gezien = [], set()
        for it in items:
            if it["soort"] == "vooraankondiging":
                ok = True
            else:
                kand = [d for d in (it["einddatum"], it["einddatum_max"]) if d]
                ok = any(vandaag.isoformat() <= d <= grens for d in kand)
            sl = dedup_sleutel(it)
            if ok and sl not in gezien:
                gezien.add(sl)
                gekozen.append(it)
        gekozen.sort(key=lambda x: (x["einddatum"] if x["einddatum"] and x["einddatum"] >= vandaag.isoformat() else x["einddatum_max"]) or "9999")
        log.info("Coming up %s: %d van %d binnen %d maanden", lst["id"], len(gekozen), len(items), maanden)
        resultaat.append({"id": lst["id"], "naam": lst["naam"], "maanden": maanden, "items": gekozen})
    db.execute("INSERT OR REPLACE INTO meta VALUES('coming_up', ?)", (json.dumps(resultaat, ensure_ascii=False),))
    db.commit()
    return resultaat


# ---------------------------------------------------------------- matching


class Matcher:
    def __init__(self, profiel):
        self.p = profiel
        self.kw = [self._re(t) for t in profiel.get("trefwoorden", [])]
        self.uit = [self._re(t) for t in profiel.get("uitsluiten", [])]
        self.cpv = [c for c in profiel.get("cpv", [])]
        self.cpv_uit = [c for c in profiel.get("cpv_uitsluiten", [])]

    @staticmethod
    def _re(term):
        prefix = term.endswith("*")
        t = term.rstrip("*")
        hoofdl = len(t) <= 4 and t.isupper()
        patroon = r"(?<![\w])" + re.escape(t if hoofdl else vouw(t)) + ("" if prefix else r"(?![\w])")
        return (re.compile(patroon), hoofdl, term)

    def _zoek(self, regels, tekst_ruw, tekst_vouw):
        for rx, hoofdl, term in regels:
            if rx.search(tekst_ruw if hoofdl else tekst_vouw):
                return term
        return None

    def match(self, r):
        """Geeft een reden (string) terug of None."""
        p = self.p
        if p.get("soort") and r["soort"] not in p["soort"]:
            return None
        ruw = "%s \n %s" % (r["titel"], r["beschrijving"])
        gv = vouw(ruw)
        if self._zoek(self.uit, ruw, gv):
            return None
        regel = p.get("regel")
        if regel == "niet_gegund":
            return r["reden_niet_gegund"] or "niet gegund" if r["niet_gegund"] else None
        if regel == "eenmalige_levering":
            if r["aard"] != "supplies" or r["raamovereenkomst"]:
                return None
            if any(c.startswith(x) for c in r["cpv"] for x in self.cpv_uit):
                return None
            return "levering zonder raamovereenkomst"
        for c in r["cpv"]:
            for x in self.cpv:
                if c.startswith(x):
                    return "CPV " + c
        term = self._zoek(self.kw, ruw, gv)
        if term:
            return "trefwoord '%s'" % term
        return None


# ---------------------------------------------------------------- opslag


def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DATA_DIR, "aanbestedingen.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS notices(
        id TEXT PRIMARY KEY, bron TEXT, gepubliceerd TEXT, eerst_gezien TEXT, profielen TEXT, json TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS xmlcache(pub TEXT PRIMARY KEY, json TEXT)")
    return db


def meta(db, k, v=None):
    if v is None:
        row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))


# ---------------------------------------------------------------- output


def schrijf_output(db, cfg, run_info):
    os.makedirs(OUT_DIR, exist_ok=True)
    grens = (dt.date.today() - dt.timedelta(days=cfg.get("dashboard_dagen_bewaren", 60))).isoformat()
    rijen = db.execute("SELECT json, eerst_gezien, profielen FROM notices WHERE profielen != '' AND "
                       "(gepubliceerd >= ? OR eerst_gezien >= ?) ORDER BY gepubliceerd DESC", (grens, grens)).fetchall()
    items = []
    for js, eg, prof in rijen:
        r = json.loads(js)
        r["eerst_gezien"] = eg
        r["profielen"] = json.loads(prof)
        items.append(r)
    data = {
        "gegenereerd": dt.datetime.now().isoformat(timespec="seconds"),
        "run": run_info,
        "luiken": cfg["luiken"],
        "profielen": [{"id": p["id"], "naam": p["naam"], "luik": p["luik"]} for p in cfg["profielen"]],
        "items": items,
        "coming_up": json.loads(meta(db, "coming_up") or "[]"),
    }
    pad = os.path.join(OUT_DIR, "data.json")
    with open(pad + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(pad + ".tmp", pad)
    sjabloon = os.path.join(BASIS, "dashboard.html")
    if os.path.exists(sjabloon):
        with open(sjabloon, encoding="utf-8") as f:
            h = f.read()
        ingebed = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        h = h.replace("/*__DATA__*/null", ingebed, 1)
        h = ('<!doctype html>\n<html lang="nl"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>\n' + h + "\n</body></html>")
        with open(os.path.join(OUT_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(h)
    return len(items)


def macos_melding(titel, tekst):
    if sys.platform != "darwin":
        return
    script = 'display notification %s with title %s sound name "Glass"' % (json.dumps(tekst), json.dumps(titel))
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    except Exception as e:  # noqa
        log.warning("Melding mislukt: %s", e)


# ---------------------------------------------------------------- hoofdprogramma


def herbereken(db, cfg):
    matchers = [Matcher(p) for p in cfg["profielen"]]
    n_weg = n_ok = 0
    for rid, js in db.execute("SELECT id, json FROM notices WHERE profielen != ''").fetchall():
        r = json.loads(js)
        if r.get("bron") == "TED":
            r["titel"] = ted_titel(r["titel"])
            if r.get("niet_gegund"):
                code = r.get("reden_code", "")
                r["reden_niet_gegund"] = REDEN_LABEL.get(code, "Niet gegund (code %s)" % code) + \
                    (" (enkele percelen)" if "enkele percelen" in r.get("reden_niet_gegund", "") else "")
        treffers = {m.p["id"]: m.match(r) for m in matchers}
        treffers = {k: v for k, v in treffers.items() if v}
        if r.get("niet_gegund"):
            r["sectoren"] = [m.p["id"] for m in matchers if not m.p.get("regel")
                             and m.match(dict(r, soort=(m.p.get("soort") or ["oproep"])[0]))]
        if treffers:
            n_ok += 1
        else:
            n_weg += 1
        db.execute("UPDATE notices SET json=?, profielen=? WHERE id=?",
                   (json.dumps(r, ensure_ascii=False), json.dumps(treffers, ensure_ascii=False) if treffers else "", rid))
    db.commit()
    run_info = {}
    pad = os.path.join(OUT_DIR, "laatste_run.json")
    if os.path.exists(pad):
        with open(pad, encoding="utf-8") as f:
            run_info = json.load(f)
    totaal = schrijf_output(db, cfg, run_info)
    log.info("Herberekend: %d treffers behouden, %d vervallen; %d in dashboard", n_ok, n_weg, totaal)


def main():
    ap = argparse.ArgumentParser(description="Aanbestedingsmonitor")
    ap.add_argument("--dagen", type=int, help="Kijk N dagen terug (standaard: sinds laatste run)")
    ap.add_argument("--alleen", help="Eén bron: ted, tenderned, boamp, pmp_lu, coming_up")
    ap.add_argument("--geen-melding", action="store_true")
    ap.add_argument("--herbereken", action="store_true",
                    help="Toets opgeslagen treffers opnieuw aan config.json (zonder downloaden) en bouw het dashboard opnieuw")
    ap.add_argument("--config", default=os.path.join(BASIS, "config.json"))
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(LOG_DIR, "monitor.log"), encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    db = open_db()
    vandaag = dt.date.today()
    laatste = meta(db, "laatste_run_datum")
    if args.dagen:
        van = vandaag - dt.timedelta(days=args.dagen)
    elif laatste:
        van = min(dt.date.fromisoformat(laatste) - dt.timedelta(days=1), vandaag - dt.timedelta(days=1))
    else:
        van = vandaag - dt.timedelta(days=cfg.get("eerste_run_dagen_terug", 7))
    log.info("Run van %s tot %s", van, vandaag)

    if args.herbereken:
        herbereken(db, cfg)
        return 0
    bekend = set(x[0] for x in db.execute("SELECT id FROM notices"))
    bronnen = {
        "ted": lambda: haal_ted(cfg, van, vandaag),
        "tenderned": lambda: haal_tenderned(cfg, van, vandaag, bekend),
        "boamp": lambda: haal_boamp(cfg, van, vandaag),
        "pmp_lu": lambda: haal_pmp_lu(cfg, van, vandaag),
    }
    matchers = [Matcher(p) for p in cfg["profielen"]]
    nu = dt.datetime.now().isoformat(timespec="seconds")
    status, nieuw_per_profiel, nieuwe_items = {}, {}, 0
    dedup = {}
    for row in db.execute("SELECT id, json FROM notices WHERE profielen != ''"):
        dedup.setdefault(dedup_sleutel(json.loads(row[1])), row[0])

    for naam, fn in bronnen.items():
        if not cfg["bronnen"].get(naam) or (args.alleen and args.alleen != naam):
            continue
        try:
            recs = fn()
            status[naam] = {"ok": True, "aantal": len(recs)}
        except Exception as e:  # noqa
            log.exception("Bron %s mislukt", naam)
            status[naam] = {"ok": False, "fout": str(e)[:300]}
            continue
        for r in recs:
            treffers = {}
            for m in matchers:
                reden = m.match(r)
                if reden:
                    treffers[m.p["id"]] = reden
            if r["id"] in bekend:
                continue  # al verwerkt in een eerdere run
            if r["niet_gegund"]:
                # In welke van je sectoren valt deze niet-gegunde opdracht?
                r["sectoren"] = [m.p["id"] for m in matchers if not m.p.get("regel")
                                 and m.match(dict(r, soort=(m.p.get("soort") or ["oproep"])[0]))]
            if treffers:
                sl = dedup_sleutel(r)
                if sl in dedup and dedup[sl] != r["id"]:
                    r["dubbel_van"] = dedup[sl]
                else:
                    dedup[sl] = r["id"]
                    nieuwe_items += 1
                    for pid in treffers:
                        nieuw_per_profiel[pid] = nieuw_per_profiel.get(pid, 0) + 1
            db.execute("INSERT INTO notices VALUES(?,?,?,?,?,?)",
                       (r["id"], r["bron"], r["gepubliceerd"], nu,
                        json.dumps(treffers, ensure_ascii=False) if treffers else "",
                        json.dumps(r, ensure_ascii=False) if treffers else "{}"))
            bekend.add(r["id"])
        db.commit()

    if cfg.get("coming_up") and (not args.alleen or args.alleen == "coming_up"):
        try:
            cu = haal_coming_up(cfg, db)
            status["coming_up"] = {"ok": True, "aantal": sum(len(x["items"]) for x in cu)}
        except Exception as e:  # noqa
            log.exception("Coming up mislukt")
            status["coming_up"] = {"ok": False, "fout": str(e)[:300]}

    if any(s.get("ok") for s in status.values()):
        meta(db, "laatste_run_datum", vandaag.isoformat())
    meta(db, "laatste_run", nu)
    db.commit()
    run_info = {"tijdstip": nu, "van": van.isoformat(), "tot": vandaag.isoformat(), "bronnen": status,
                "nieuw": nieuwe_items, "nieuw_per_profiel": nieuw_per_profiel}
    totaal = schrijf_output(db, cfg, run_info)
    with open(os.path.join(OUT_DIR, "laatste_run.json"), "w", encoding="utf-8") as f:
        json.dump(run_info, f, ensure_ascii=False, indent=2)
    log.info("Klaar: %d nieuwe relevante publicaties; %d in dashboard. Status: %s", nieuwe_items, totaal, status)

    if cfg.get("macos_melding", True) and not args.geen_melding:
        namen = {p["id"]: p["naam"] for p in cfg["profielen"]}
        fouten = [k for k, s in status.items() if not s.get("ok")]
        if nieuwe_items:
            top = sorted(nieuw_per_profiel.items(), key=lambda x: -x[1])[:3]
            tekst = ", ".join("%s: %d" % (namen.get(k, k), v) for k, v in top)
        else:
            tekst = "Geen nieuwe relevante publicaties."
        if fouten:
            tekst += " Let op: bron(nen) mislukt: " + ", ".join(fouten)
        macos_melding("Aanbestedingen: %d nieuw" % nieuwe_items, tekst)
    return 0 if any(s.get("ok") for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
