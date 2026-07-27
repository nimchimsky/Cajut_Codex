#!/usr/bin/env python3
"""Download the supplemental official corpus for Bombers Generalitat 81/26.

Scope:
- historical questionnaires and answer keys for 81/18, 81/19, 81/19.2 and 81/21;
- primary legal texts for official topics 1-7;
- selected open-licence operational manuals from Interior's public repository.

Only publicly accessible official sources are used. The script records HTTP status,
final URL, size and SHA-256 for every attempted download.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(os.environ.get("SUPPLEMENT_DIR", "supplement"))
ROOT.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150 BombersStudyCollector/2.0",
    "Accept-Language": "ca,es;q=0.9,en;q=0.7",
})
TIMEOUT = 75
DOC_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ods", ".odt", ".rtf",
    ".csv", ".zip", ".7z", ".rar", ".txt", ".ppt", ".pptx", ".jpg",
    ".jpeg", ".png", ".svg",
}


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


records: list[Record] = []
seen_urls: set[str] = set()
used_paths: set[str] = set()


def slug(value: str, max_length: int = 170) -> str:
    value = html.unescape(unquote(value or "")).strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"[^A-Za-z0-9._()\- ]+", "-", value)
    value = re.sub(r"\s+", "_", value).strip("._-")
    return (value or "document")[:max_length]


def unique_path(section: str, filename: str) -> Path:
    folder = ROOT / section
    folder.mkdir(parents=True, exist_ok=True)
    filename = slug(filename)
    stem, ext = os.path.splitext(filename)
    candidate = folder / filename
    suffix = 2
    while str(candidate).lower() in used_paths or candidate.exists():
        candidate = folder / f"{stem}_{suffix}{ext}"
        suffix += 1
    used_paths.add(str(candidate).lower())
    return candidate


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def request(url: str, referer: str = "") -> requests.Response:
    last_error: Exception | None = None
    headers = {"Referer": referer} if referer else None
    for attempt in range(4):
        try:
            response = SESSION.get(
                url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True
            )
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def response_filename(response: requests.Response, original_url: str, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    name = Path(unquote(urlparse(response.url or original_url).path)).name
    return name or fallback


def download(
    url: str,
    section: str,
    *,
    source_page: str = "",
    title: str = "",
    preferred_name: str = "",
    force: bool = False,
) -> Record | None:
    url = url.strip().split("#", 1)[0]
    if not url or url.startswith(("mailto:", "javascript:", "tel:")):
        return None
    if url in seen_urls and not force:
        return None
    seen_urls.add(url)
    try:
        response = request(url, source_page)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if response.status_code >= 400:
            rec = Record(section, source_page, url, response.url, title, "", "error",
                         response.status_code, content_type, 0, "", "HTTP error")
            records.append(rec)
            return rec
        filename = preferred_name or response_filename(response, url, slug(title or "document"))
        if content_type == "application/pdf" and not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        elif content_type == "text/html" and not filename.lower().endswith((".html", ".htm")):
            filename += ".html"
        out = unique_path(section, filename)
        size = 0
        with out.open("wb") as f:
            for chunk in response.iter_content(256 * 1024):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
        rec = Record(section, source_page, url, response.url, title,
                     str(out.relative_to(ROOT)), "downloaded", response.status_code,
                     content_type, size, digest(out))
        records.append(rec)
        return rec
    except Exception as exc:  # noqa: BLE001
        rec = Record(section, source_page, url, "", title, "", "error", "", "", 0, "", repr(exc))
        records.append(rec)
        return rec


def is_downloadable(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOC_EXTENSIONS) or any(
        marker in path for marker in (
            "/bitstream/", "/bitstreams/", "/download/", "/content",
            "pdfproviderservlet", "utilseadop/pdf",
        )
    )


def snapshot_and_download_links(url: str, section: str) -> None:
    """Save an official page, its link index and every directly linked document."""
    try:
        response = request(url)
    except Exception as exc:  # noqa: BLE001
        records.append(Record(section, url, url, "", "", "", "error", "", "", 0, "", repr(exc)))
        return
    raw = response.content
    page_name = slug(Path(urlparse(url).path.rstrip("/")).name or "index") + ".html"
    page_path = unique_path(section, page_name)
    page_path.write_bytes(raw)
    records.append(Record(section, url, url, response.url, "Official page snapshot",
                          str(page_path.relative_to(ROOT)), "downloaded", response.status_code,
                          response.headers.get("content-type", ""), len(raw), digest(page_path)))
    soup = BeautifulSoup(raw, "lxml")
    links: list[tuple[str, str]] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(response.url, href).split("#", 1)[0]
        label = " ".join(anchor.get_text(" ", strip=True).split())
        links.append((absolute, label))
        if is_downloadable(absolute):
            download(absolute, section, source_page=url, title=label)
    index_path = unique_path(section, "INDEX_ENLLACOS.csv")
    with index_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "text"])
        writer.writerows(links)


def write_url_shortcut(section: str, name: str, url: str) -> None:
    path = unique_path(section, slug(name) + ".url")
    path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")


def dspace_item(handle_id: int, section: str) -> None:
    """Download original PDF bitstreams from a DSpace 7 item by persistent handle."""
    handle = f"20.500.14007/{handle_id}"
    page_url = f"https://dsp.interior.gencat.cat/handle/{handle}"
    write_url_shortcut(section, f"repositori_{handle_id}", page_url)
    try:
        page = request(page_url)
        raw = page.content
        snapshot = unique_path(section, f"repositori_{handle_id}.html")
        snapshot.write_bytes(raw)
        records.append(Record(section, page_url, page_url, page.url, "DSpace item page",
                              str(snapshot.relative_to(ROOT)), "downloaded", page.status_code,
                              page.headers.get("content-type", ""), len(raw), digest(snapshot)))
        soup = BeautifulSoup(raw, "lxml")
        candidates: list[tuple[str, str]] = []
        for anchor in soup.select("a[href]"):
            absolute = urljoin(page.url, anchor.get("href", ""))
            label = " ".join(anchor.get_text(" ", strip=True).split())
            if is_downloadable(absolute) or ".pdf" in absolute.lower():
                candidates.append((absolute, label))
        for match in re.finditer(r'https?://[^"\'<> ]+\.pdf(?:\?[^"\'<> ]*)?', raw.decode("utf-8", "ignore"), re.I):
            candidates.append((html.unescape(match.group(0)), "PDF"))
        downloaded = 0
        for candidate, label in candidates:
            before = len(records)
            rec = download(candidate, section, source_page=page_url, title=label)
            if rec and rec.status == "downloaded" and rec.content_type == "application/pdf":
                downloaded += 1
        if downloaded:
            return
    except Exception:
        pass

    # Fallback to DSpace REST API.
    try:
        api_url = (
            "https://dsp.interior.gencat.cat/server/api/core/items/search/findByHandle"
            f"?handle={handle}"
        )
        item = SESSION.get(api_url, timeout=TIMEOUT).json()
        bundles_url = item.get("_links", {}).get("bundles", {}).get("href", "")
        if not bundles_url:
            return
        bundles = SESSION.get(bundles_url, timeout=TIMEOUT).json().get("_embedded", {}).get("bundles", [])
        for bundle in bundles:
            if bundle.get("name") != "ORIGINAL":
                continue
            bitstreams_url = bundle.get("_links", {}).get("bitstreams", {}).get("href", "")
            bitstreams = SESSION.get(bitstreams_url, timeout=TIMEOUT).json().get("_embedded", {}).get("bitstreams", [])
            for bitstream in bitstreams:
                content_url = bitstream.get("_links", {}).get("content", {}).get("href", "")
                name = bitstream.get("name") or f"repositori_{handle_id}.pdf"
                if content_url:
                    download(content_url, section, source_page=page_url, title=name,
                             preferred_name=name, force=True)
    except Exception as exc:  # noqa: BLE001
        records.append(Record(section, page_url, page_url, "", "DSpace REST fallback", "",
                              "error", "", "", 0, "", repr(exc)))


def main() -> int:
    historical_pages = [
        ("04_EXAMENS_I_PLANTILLES/2018_81-18",
         "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/acces-a-bomber-a-de-lescala-basica-81-18/"),
        ("04_EXAMENS_I_PLANTILLES/2019_81-19",
         "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/Acces-a-bomber-a-de-lescala-basica-81_19/"),
        ("04_EXAMENS_I_PLANTILLES/2019-2_81-19.2",
         "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/acces-a-bomber-a-de-lescala-basica-81-19.2/"),
        ("04_EXAMENS_I_PLANTILLES/2021_81-21",
         "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/acces-a-bomber-a-de-lescala-basica-81-21/index.html"),
    ]
    for section, page in historical_pages:
        print(f"Historical page: {page}", flush=True)
        snapshot_and_download_links(page, section)

    legal_documents = [
        # Topic 1
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Constitucio_espanyola_consolidada.pdf", "https://www.boe.es/buscar/pdf/1978/BOE-A-1978-31229-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Estatut_autonomia_Catalunya_LO_6-2006_consolidat.pdf", "https://www.boe.es/buscar/pdf/2006/BOE-A-2006-13087-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Llei_13-2008_presidencia_Generalitat_i_Govern_consolidada.pdf", "https://www.boe.es/buscar/pdf/2009/BOE-A-2009-2009-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Llei_26-2010_regim_juridic_administracions_Catalunya_consolidada.pdf", "https://www.boe.es/buscar/pdf/2010/BOE-A-2010-13883-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Llei_40-2015_regim_juridic_sector_public_consolidada.pdf", "https://www.boe.es/buscar/pdf/2015/BOE-A-2015-10566-consolidado.pdf"),
        # Topic 2
        ("03_NORMATIVA_T01-T07/T02_Funcio_publica", "TREBEP_RDL_5-2015_consolidat.pdf", "https://www.boe.es/buscar/pdf/2015/BOE-A-2015-11719-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T02_Funcio_publica", "Decret_legislatiu_1-1997_funcio_publica_Catalunya_consolidat.pdf", "https://www.boe.es/buscar/pdf/1997/DOGC-f-1997-90001-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T02_Funcio_publica", "Llei_21-1987_incompatibilitats_Catalunya_consolidada.pdf", "https://www.boe.es/buscar/pdf/1987/BOE-A-1987-28478-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T02_Funcio_publica", "Llei_53-1984_incompatibilitats_sector_public_consolidada.pdf", "https://www.boe.es/buscar/pdf/1985/BOE-A-1985-151-consolidado.pdf"),
        # Topic 3
        ("03_NORMATIVA_T01-T07/T03_PRL_i_EPI", "Llei_31-1995_prevencio_riscos_laborals_consolidada.pdf", "https://www.boe.es/buscar/pdf/1995/BOE-A-1995-24292-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T03_PRL_i_EPI", "RD_773-1997_equips_proteccio_individual_consolidat.pdf", "https://www.boe.es/buscar/pdf/1997/BOE-A-1997-12735-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T03_PRL_i_EPI", "Reglament_UE_2016-425_EPI.pdf", "https://eur-lex.europa.eu/legal-content/ES/TXT/PDF/?uri=CELEX:32016R0425"),
        # Topic 4
        ("03_NORMATIVA_T01-T07/T04_Igualtat_i_no_discriminacio", "Llei_19-2020_igualtat_tracte_no_discriminacio_consolidada.pdf", "https://www.boe.es/buscar/pdf/2021/BOE-A-2021-1663-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T04_Igualtat_i_no_discriminacio", "Llei_17-2015_igualtat_efectiva_dones_homes_consolidada.pdf", "https://www.boe.es/buscar/pdf/2015/BOE-A-2015-9676-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T04_Igualtat_i_no_discriminacio", "Protocol_assetjament_sexual_i_per_raons_de_genere_2023.pdf", "https://igualtat.gencat.cat/web/.content/Ambits/violencies-masclistes/NOVA-ESTRUCTURA/Protocols/protocol-assetjament-sexual.pdf"),
        # Topic 5
        ("03_NORMATIVA_T01-T07/T05_Politica_linguistica", "Llei_1-1998_politica_linguistica_consolidada.pdf", "https://www.boe.es/buscar/pdf/1998/BOE-A-1998-2989-consolidado.pdf"),
        # Topic 6
        ("03_NORMATIVA_T01-T07/T06_Bombers_i_proteccio_civil", "Llei_5-1994_serveis_prevencio_extincio_incendis_salvaments_consolidada.pdf", "https://www.boe.es/buscar/pdf/1994/BOE-A-1994-12665-consolidado.pdf"),
        ("03_NORMATIVA_T01-T07/T06_Bombers_i_proteccio_civil", "Llei_4-1997_proteccio_civil_Catalunya_consolidada.pdf", "https://www.boe.es/buscar/pdf/1997/BOE-A-1997-14409-consolidado.pdf"),
    ]
    for section, name, url in legal_documents:
        print(f"Legal document: {name}", flush=True)
        download(url, section, title=name, preferred_name=name, force=True)

    # Topic 7 and current consolidated Catalan texts: preserve authoritative Portal Jurídic links.
    portal_links = [
        ("03_NORMATIVA_T01-T07/T01_Constitucio_EAC_i_institucions", "Portal_Juridic_Llei_13-2008", "https://portaljuridic.gencat.cat/eli/es-ct/l/2008/11/05/13"),
        ("03_NORMATIVA_T01-T07/T02_Funcio_publica", "Portal_Juridic_Decret_legislatiu_1-1997", "https://portaljuridic.gencat.cat/eli/es-ct/dlg/1997/10/31/1"),
        ("03_NORMATIVA_T01-T07/T07_Guardia_comandament_i_estructura", "Portal_Juridic_Decret_276-2016", "https://portaljuridic.gencat.cat/eli/es-ct/d/2016/07/19/276"),
        ("03_NORMATIVA_T01-T07/T07_Guardia_comandament_i_estructura", "Portal_Juridic_Decret_12-2023", "https://portaljuridic.gencat.cat/eli/es-ct/d/2023/01/24/12"),
        ("03_NORMATIVA_T01-T07/T07_Guardia_comandament_i_estructura", "DOGC_Decret_41-2024_modificacio", "https://dogc.gencat.cat/ca/document-del-dogc/?documentId=978714"),
        ("03_NORMATIVA_T01-T07/T07_Guardia_comandament_i_estructura", "DOGC_Decret_239-2025_modificacio", "https://dogc.gencat.cat/ca/document-del-dogc/?documentId=1026782"),
        ("03_NORMATIVA_T01-T07/T07_Guardia_comandament_i_estructura", "DOGC_Decret_16-2026_modificacio", "https://dogc.gencat.cat/ca/document-del-dogc/?documentId=1044533"),
        ("03_NORMATIVA_T01-T07/T06_Bombers_i_proteccio_civil", "Portal_Juridic_Llei_5-1994", "https://portaljuridic.gencat.cat/eli/es-ct/l/1994/05/04/5"),
        ("03_NORMATIVA_T01-T07/T06_Bombers_i_proteccio_civil", "Portal_Juridic_Llei_4-1997", "https://portaljuridic.gencat.cat/eli/es-ct/l/1997/05/20/4"),
    ]
    for section, name, url in portal_links:
        write_url_shortcut(section, name, url)
        download(url, section, title=name, preferred_name=name + ".html", force=True)

    # Open official reference manuals that add operational depth beyond the 27 study manuals.
    dspace_ids = [
        2913, 2914, 2916, 2928, 2933, 2935, 2938, 2940, 2941, 2942,
        2943, 2944, 2945, 3022, 3023, 3297, 3378, 4348,
    ]
    for item_id in dspace_ids:
        print(f"DSpace item: {item_id}", flush=True)
        dspace_item(item_id, "06_MATERIAL_COMPLEMENTARI_OFICIAL/Manuals_operatius_i_guies")

    # Monitoring shortcuts.
    monitoring = {
        "Pàgina_oficial_81-26": "https://interior.gencat.cat/ca/arees_dactuacio/bombers/convocatories/bombers-funcionaris/acces-a-bomber-a-de-1a-de-lescala-tecnica-81-26/",
        "DOGC": "https://dogc.gencat.cat/",
        "e-Tauler": "https://tauler.seu-e.cat/",
        "Portal_Juridic": "https://portaljuridic.gencat.cat/",
        "Repositori_Interior_Bombers": "https://dsp.interior.gencat.cat/handle/20.500.14007/24",
    }
    for name, url in monitoring.items():
        write_url_shortcut("99_ENLLACOS_ACTUALITZACIO", name, url)

    manifest = ROOT / "00_LLEGIU-ME" / "MANIFEST_SUPLEMENT.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(Record("", "", "", "", "", "", "", "", "", 0, "")).keys())
    with manifest.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(asdict(rec))

    errors = [r for r in records if r.status != "downloaded"]
    error_path = ROOT / "00_LLEGIU-ME" / "ERRORS_SUPLEMENT.txt"
    with error_path.open("w", encoding="utf-8") as f:
        if not errors:
            f.write("No s'han registrat errors.\n")
        for rec in errors:
            f.write(f"[{rec.http_status}] {rec.url}\n  {rec.notes}\n")

    all_files = [p for p in ROOT.rglob("*") if p.is_file()]
    total_size = sum(p.stat().st_size for p in all_files)
    summary = ROOT / "00_LLEGIU-ME" / "RESUM_SUPLEMENT.txt"
    summary.write_text(
        f"Fitxers: {len(all_files)}\nMida: {total_size} bytes\n"
        f"Registres: {len(records)}\nErrors: {len(errors)}\n",
        encoding="utf-8",
    )
    print(summary.read_text(), flush=True)
    # Do not fail the artifact upload for non-critical stale links.
    return 0


if __name__ == "__main__":
    sys.exit(main())
