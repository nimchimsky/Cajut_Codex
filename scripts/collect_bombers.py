#!/usr/bin/env python3
"""Collect publicly available official study material for Bombers Generalitat.

The script is deliberately conservative: it downloads documents directly linked
from official pages, preserves source URLs, records hashes and never attempts to
bypass authentication, paywalls or robots restrictions.
"""

from __future__ import annotations

import csv
import hashlib
import html
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(os.environ.get("BUNDLE_DIR", "bundle"))
ROOT.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/150 Safari/537.36 BombersStudyCollector/1.0"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ca,en;q=0.8"})
TIMEOUT = 60
DOC_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ods", ".odt", ".rtf",
    ".csv", ".zip", ".7z", ".rar", ".txt", ".ppt", ".pptx", ".jpg",
    ".jpeg", ".png", ".svg",
}
DOC_CONTENT_PREFIXES = (
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats", "application/vnd.ms-",
    "application/zip", "application/x-7z", "application/x-rar",
    "text/csv", "text/plain", "image/",
)


@dataclass
class Record:
    section: str
    source_page: str
    url: str
    final_url: str
    title: str
    local_path: str
    status: str
    http_status: int | str
    content_type: str
    size_bytes: int
    sha256: str
    notes: str = ""


RECORDS: list[Record] = []
SEEN_URLS: set[str] = set()
USED_PATHS: set[str] = set()


def slug(text: str, max_len: int = 150) -> str:
    text = html.unescape(unquote(text or "")).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"[^A-Za-z0-9._()\- ]+", "-", text)
    text = re.sub(r"\s+", "_", text).strip("._-")
    return (text or "document")[:max_len]


def unique_path(section: str, filename: str) -> Path:
    folder = ROOT / section
    folder.mkdir(parents=True, exist_ok=True)
    filename = slug(filename)
    stem, ext = os.path.splitext(filename)
    candidate = folder / filename
    n = 2
    while str(candidate).lower() in USED_PATHS or candidate.exists():
        candidate = folder / f"{stem}_{n}{ext}"
        n += 1
    USED_PATHS.add(str(candidate).lower())
    return candidate


def filename_from_response(resp: requests.Response, url: str, fallback: str = "document") -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    name = Path(unquote(urlparse(resp.url or url).path)).name
    return name or fallback


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_doc(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOC_EXTS) or any(
        marker in path for marker in ("pdfproviderservlet", "/bitstream/", "/download/")
    )


def get(url: str, *, referer: str | None = None) -> requests.Response:
    headers = {"Referer": referer} if referer else None
    last: Exception | None = None
    for attempt in range(4):
        try:
            resp = SESSION.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True)
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    assert last is not None
    raise last


def save_response(
    resp: requests.Response,
    *,
    url: str,
    section: str,
    source_page: str,
    title: str = "",
    preferred_name: str | None = None,
) -> Record:
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    status = resp.status_code
    if status >= 400:
        rec = Record(section, source_page, url, resp.url, title, "", "error", status, ctype, 0, "", "HTTP error")
        RECORDS.append(rec)
        return rec

    filename = preferred_name or filename_from_response(resp, resp.url, fallback=title or "document")
    if ctype == "text/html" and not filename.lower().endswith(('.html', '.htm')):
        filename += ".html"
    elif ctype == "application/pdf" and not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    out = unique_path(section, filename)
    size = 0
    with out.open("wb") as f:
        for chunk in resp.iter_content(1024 * 256):
            if chunk:
                f.write(chunk)
                size += len(chunk)
    digest = sha256_file(out)
    rec = Record(
        section, source_page, url, resp.url, title, str(out.relative_to(ROOT)),
        "downloaded", status, ctype, size, digest,
    )
    RECORDS.append(rec)
    return rec


def download(
    url: str,
    section: str,
    *,
    source_page: str = "",
    title: str = "",
    preferred_name: str | None = None,
    force: bool = False,
) -> Record | None:
    clean = url.split("#", 1)[0].strip()
    if not clean or clean.startswith(("mailto:", "javascript:", "tel:")):
        return None
    key = clean
    if key in SEEN_URLS and not force:
        return None
    SEEN_URLS.add(key)
    try:
        resp = get(clean, referer=source_page or None)
        return save_response(
            resp, url=clean, section=section, source_page=source_page,
            title=title, preferred_name=preferred_name,
        )
    except Exception as exc:  # noqa: BLE001
        rec = Record(section, source_page, clean, "", title, "", "error", "", "", 0, "", repr(exc))
        RECORDS.append(rec)
        return rec


def collect_page(seed_url: str, section: str, *, include_html_targets: bool = True) -> None:
    """Save an official page and every directly linked downloadable document."""
    try:
        resp = get(seed_url)
    except Exception as exc:  # noqa: BLE001
        RECORDS.append(Record(section, seed_url, seed_url, "", "", "", "error", "", "", 0, "", repr(exc)))
        return

    raw = resp.content
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].lower()
    page_name = slug(Path(urlparse(seed_url).path.rstrip("/")).name or "index") + ".html"
    page_path = unique_path(section, page_name)
    page_path.write_bytes(raw)
    RECORDS.append(Record(
        section, seed_url, seed_url, resp.url, "Official page snapshot",
        str(page_path.relative_to(ROOT)), "downloaded", resp.status_code, ctype,
        len(raw), sha256_file(page_path),
    ))

    soup = BeautifulSoup(raw, "lxml")
    link_rows: list[tuple[str, str]] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(resp.url, href).split("#", 1)[0]
        text = " ".join(a.get_text(" ", strip=True).split())
        link_rows.append((absolute, text))

    links_csv = unique_path(section, "enllacos_pagina.csv")
    with links_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["url", "text"])
        w.writerows(link_rows)

    official_hosts = {
        "interior.gencat.cat", "portaldogc.gencat.cat", "dogc.gencat.cat",
        "tauler.seu-e.cat", "seu-e.cat", "web.gencat.cat", "tramits.gencat.cat",
        "portaljuridic.gencat.cat", "dsp.interior.gencat.cat", "hdl.handle.net",
        "govern.cat", "ispc.gencat.cat", "boe.es", "www.boe.es", "eur-lex.europa.eu",
    }
    for absolute, text in link_rows:
        host = urlparse(absolute).hostname or ""
        if looks_like_doc(absolute):
            download(absolute, section, source_page=seed_url, title=text)
        elif include_html_targets and host in official_hosts:
            # Preserve linked official notices/pages as offline HTML snapshots.
            # Limit to documents, process notices, DOGC, e-Tauler and repository items.
            path_lower = urlparse(absolute).path.lower()
            relevant = any(token in path_lower for token in (
                "convoc", "bombers", "document", "utilsEADOP".lower(), "handle/20.500.14007",
                "tauler", "dogc", "portaljuridic", "actuacions-administratives",
            ))
            if relevant:
                download(absolute, section, source_page=seed_url, title=text)


def write_url_file(section: str, name: str, url: str) -> None:
    path = unique_path(section, slug(name) + ".url")
    path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")


def main() -> int:
    # Core official process pages.
    seeds = [
        (
            "01_81-26_OFICIAL/00_Programa_i_guia",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/acces-a-bomber-a-de-1a-de-lescala-tecnica-81-26/",
        ),
        (
            "02_CONVOCATORIES_HISTORIQUES/81-25",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/Acces-a-bomber_a-de-1a-de-lescala-tecnica-81_25/",
        ),
        (
            "02_CONVOCATORIES_HISTORIQUES/81-24",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/Acces-a-bomber_a-de-1a-de-lescala-tecnica-81_24/",
        ),
        (
            "02_CONVOCATORIES_HISTORIQUES/81-23",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/Acces-a-bomber_a-de-1a-de-lescala-tecnica-81_23/",
        ),
        (
            "02_CONVOCATORIES_HISTORIQUES/00_INDEX",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/",
        ),
        (
            "08_MATERIAL_COMPLEMENTARI/Informacio_general",
            "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/informacio_general/Informacio-general-de-bombers-funcionaris/",
        ),
    ]
    for section, url in seeds:
        print(f"Collecting page: {url}", flush=True)
        collect_page(url, section)

    # Program and last complete bases are fixed, high-priority documents.
    fixed = [
        (
            "01_81-26_OFICIAL/00_Programa_i_guia",
            "https://portaldogc.gencat.cat/utilsEADOP/PDF/9709/2161979.pdf",
            "2026-07-16_Resolucio_ISP-2428-2026_Programa_34_temes.pdf",
            "Programa oficial 2026",
        ),
        (
            "02_CONVOCATORIES_HISTORIQUES/81-25",
            "https://portaldogc.gencat.cat/utilsEADOP/AppJava/PdfProviderServlet?documentId=1009934&language=ca_ES&type=01",
            "2025-03-26_Resolucio_ISP-1004-2025_Bases_81-25.pdf",
            "Bases 81/25",
        ),
    ]
    for section, url, name, title in fixed:
        download(url, section, title=title, preferred_name=name, force=True)

    # Explicitly ensure all 27 official study manuals (T08-T34) are present.
    codes = {
        8: "MAN.GRAL.008", 9: "MAN.GRAL.003", 10: "MAN.GRAL.004",
        11: "MAN.GRAL.005", 12: "MAN.GRAL.007", 13: "MAN.GRAL.009",
        14: "MAN.GRAL.002", 15: "MAN.GRAL.006", 16: "MAN.ATXX.000",
        17: "MAN.COMU.000", 18: "MAN.EQIP.000", 19: "MAN.EQIP.002",
        20: "MAN.SEGU.000", 21: "MAN.GRAL.001", 22: "MAN.COMD.001",
        23: "MAN.SP.000", 24: "MAN.INES.000", 25: "MAN.INVE.000",
        26: "MAN.INXX.000", 27: "MAN.RRTT.000", 28: "MAN.SANI.000",
        29: "MAN.SANI.001", 30: "MAN.SVES.000", 31: "MAN.SVMN.000",
        32: "MAN.SVVH.000", 33: "MAN.SVXX.000", 34: "MAN.SVMN.001",
    }
    base = (
        "https://interior.gencat.cat/web/.content/home/030_arees_dactuacio/bombers/"
        "acces_al_cos_de_bombers/convocatories/convocatories_de_bombers/"
        "ACCES-81_26/DOCUMENTACIO-ESTUDI/"
    )
    for topic, code in codes.items():
        url = f"{base}Tema-{topic:02d}-{code}.pdf"
        name = f"T{topic:02d}_{code}_OFICIAL_2026.pdf"
        download(
            url, "01_81-26_OFICIAL/01_Manuals_oficials_T08-T34",
            source_page=seeds[0][1], title=f"Tema {topic} - {code}",
            preferred_name=name, force=True,
        )

    # High-value open official reference works from Interior's repository.
    dsp_handles = [
        ("Guia_estudi_bombers_voluntaris_2016", "https://dsp.interior.gencat.cat/handle/20.500.14007/3023"),
        ("Seguretat_incendis_forestals_manual_2025", "https://dsp.interior.gencat.cat/handle/20.500.14007/2938"),
        ("Installacions_suport_comandament_manual_2025", "https://dsp.interior.gencat.cat/handle/20.500.14007/2942"),
        ("Pla_Bombers_2025", "https://dsp.interior.gencat.cat/handle/20.500.14007/4348"),
        ("Repositori_Bombers", "https://dsp.interior.gencat.cat/handle/20.500.14007/24"),
        ("Monografies_Bombers", "https://dsp.interior.gencat.cat/handle/20.500.14007/152"),
        ("Informacio_institucional_Bombers", "https://dsp.interior.gencat.cat/handle/20.500.14007/142"),
    ]
    for name, url in dsp_handles:
        section = "08_MATERIAL_COMPLEMENTARI/Repositori_Interior"
        collect_page(url, section, include_html_targets=False)
        write_url_file(section, name, url)

    # Strategic and official monitoring sources.
    official_links = {
        "Pla_Bombers_2030": "https://govern.cat/gov/notes-premsa/755552/govern-aprova-pla-estrategic-bombers-2030",
        "DOGC": "https://dogc.gencat.cat/",
        "e-Tauler": "https://tauler.seu-e.cat/",
        "Portal_Juridic": "https://portaljuridic.gencat.cat/",
        "Tramits_Gencat": "https://web.gencat.cat/ca/tramits",
    }
    for name, url in official_links.items():
        write_url_file("10_ENLLACOS_I_SEGUIMENT", name, url)

    # Write manifests.
    manifest = ROOT / "00_LLEGIU-ME" / "MANIFEST_DOCUMENTS.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(Record("", "", "", "", "", "", "", "", "", 0, "")).keys())
    with manifest.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in RECORDS:
            w.writerow(asdict(rec))

    errors = [r for r in RECORDS if r.status != "downloaded"]
    with (ROOT / "00_LLEGIU-ME" / "ERRORS_DESCARREGA.txt").open("w", encoding="utf-8") as f:
        if not errors:
            f.write("No s'han registrat errors de descàrrega.\n")
        for r in errors:
            f.write(f"[{r.http_status}] {r.url}\n  {r.notes}\n")

    total_files = sum(1 for p in ROOT.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file())
    summary = ROOT / "00_LLEGIU-ME" / "RESUM_RECOLLIDA.txt"
    summary.write_text(
        f"Fitxers inclosos: {total_files}\n"
        f"Mida sense comprimir: {total_bytes} bytes\n"
        f"Registres de descàrrega: {len(RECORDS)}\n"
        f"Errors: {len(errors)}\n",
        encoding="utf-8",
    )
    print(summary.read_text(), flush=True)
    return 0 if not any(r.status == "error" and "Tema-" in r.url for r in errors) else 2


if __name__ == "__main__":
    sys.exit(main())
