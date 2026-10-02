"""Fetch a paper's open-access PDF by DOI and save it to disk.

Candidate sources, in order: caller-supplied mirrors (extra_urls), Unpaywall,
Semantic Scholar, OpenAlex, OpenAIRE. Downloads go out through curl with
browser-like headers, which is what gets past the Cloudflare challenges that
plain Python clients fail on. Every candidate is verified by its %PDF header,
so an HTML block page can never be saved as a ".pdf".
"""
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
_HDRS = [
    "-H", "Accept: application/pdf,text/html,*/*",
    "-H", "Accept-Language: en-US,en;q=0.9",
    "-H", "Sec-Fetch-Dest: document",
    "-H", "Sec-Fetch-Mode: navigate",
    "-H", "Sec-Fetch-Site: none",
    "-H", "Upgrade-Insecure-Requests: 1",
]
_DEFAULT_OUT = r"D:\Dev\Workspace\slife\slife.files\files\pdf"
_BAD = set('\\/:*?"<>|')


def _clean(text):
    return "".join("_" if c in _BAD else c for c in text).strip()[:120]


def _get_json(url, timeout=45):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw.decode("utf-8", "replace"))


def _candidates(doi, email):
    """Open-access locations for a DOI, best first, plus a note per source."""
    urls, notes = [], []
    q = urllib.parse.quote(doi)

    try:
        j = _get_json("https://api.unpaywall.org/v2/%s?email=%s" % (q, email))
        for loc in [j.get("best_oa_location")] + list(j.get("oa_locations") or []):
            if not loc:
                continue
            for u in (loc.get("url_for_pdf"), loc.get("url")):
                if u and u not in urls:
                    urls.append(u)
        notes.append("unpaywall: is_oa=%s" % j.get("is_oa"))
    except Exception as e:
        notes.append("unpaywall: ERR %s" % str(e)[:40])

    try:
        s = _get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:%s"
                      "?fields=openAccessPdf" % q)
        u = ((s or {}).get("openAccessPdf") or {}).get("url")
        if u and u not in urls:
            urls.append(u)
        notes.append("semantic-scholar: openAccessPdf=%s" % bool(u))
    except Exception as e:
        notes.append("semantic-scholar: ERR %s" % str(e)[:40])

    try:
        o = _get_json("https://api.openalex.org/works/doi:%s"
                      "?select=best_oa_location,locations" % q)
        for loc in [o.get("best_oa_location")] + list(o.get("locations") or []):
            if loc and loc.get("pdf_url") and loc["pdf_url"] not in urls:
                urls.append(loc["pdf_url"])
        notes.append("openalex: ok")
    except Exception as e:
        notes.append("openalex: ERR %s" % str(e)[:40])

    try:
        raw = _get_json("https://api.openaire.eu/search/publications"
                        "?doi=%s&format=json" % q)
        res = ((raw.get("response") or {}).get("results") or {}).get("result") or []
        if isinstance(res, dict):
            res = [res]
        for r in res:
            result = (((r.get("metadata") or {}).get("oaf:entity") or {})
                      .get("oaf:result") or {})
            insts = (result.get("children") or {}).get("instance") or []
            if isinstance(insts, dict):
                insts = [insts]
            for inst in insts:
                wrs = inst.get("webresource") or []
                if isinstance(wrs, dict):
                    wrs = [wrs]
                for wr in wrs:
                    u = (wr.get("url") or {}).get("$")
                    if u and u not in urls:
                        urls.append(u)
        notes.append("openaire: ok")
    except Exception as e:
        notes.append("openaire: ERR %s" % str(e)[:40])

    return urls, notes


def _download(url, path):
    """Try one URL. Returns (ok, info); never leaves a non-PDF behind."""
    curl = shutil.which("curl")
    if curl:
        proc = subprocess.run(
            [curl, "-sL", "--compressed", "-A", _UA] + _HDRS +
            ["--max-time", "240", "-w", "%{http_code} %{size_download}",
             "-o", path, url],
            capture_output=True, text=True)
        info = "curl http=%s" % proc.stdout.strip()[:40]
    else:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Accept": "application/pdf,text/html,*/*"})
            data = urllib.request.urlopen(req, timeout=240).read()
            with open(path, "wb") as fh:
                fh.write(data)
            info = "urllib %d bytes" % len(data)
        except Exception as e:
            return False, "urllib ERR " + str(e)[:50]
    if os.path.exists(path):
        with open(path, "rb") as fh:
            head = fh.read(1024)
        if b"%PDF" in head:
            return True, "%s bytes" % os.path.getsize(path)
        os.remove(path)
    return False, info


def fetch_oa_pdf(target: str, out_dir: str = "", name: str = "",
                 email: str = "", extra_urls: str = "") -> str:
    """Download a paper's open-access PDF by DOI and save it to disk.

    Looks for open-access copies through Unpaywall, Semantic Scholar, OpenAlex
    and OpenAIRE, then downloads the first candidate that returns a real PDF.
    Requests carry browser-like headers and go out through curl, which is what
    gets past the Cloudflare challenges plain Python clients fail on. Nothing
    is saved unless it really is a PDF.

    Args:
        target: A DOI (e.g. 10.3390/e21100944) or any URL containing one.
        out_dir: Folder to save into; empty uses <data_dir>/slife.files/files/pdf.
        name: File name to save under; empty derives one from the DOI.
        email: Contact e-mail for the Unpaywall API; empty falls back to $UNPAYWALL_EMAIL.
        extra_urls: Extra candidate URLs tried first, separated by ';' — use it for publisher mirrors (e.g. mdpi-res.com when mdpi.com is blocked).
    """
    match = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", target or "")
    if not match:
        return "Error: no DOI found in target=%r" % (target or "")
    doi = match.group(0).rstrip(".,;)")

    out_dir = out_dir or _DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, _clean(name or (doi.replace("/", "-") + ".pdf")))
    if not path.lower().endswith(".pdf"):
        path += ".pdf"

    if os.path.exists(path) and os.path.getsize(path) > 20000:
        return "Already present: %s (%d bytes)" % (path, os.path.getsize(path))

    email = email or os.environ.get("UNPAYWALL_EMAIL") or "anonymous@example.org"
    urls = [u.strip() for u in (extra_urls or "").split(";") if u.strip()]
    found, notes = _candidates(doi, email)
    urls += [u for u in found if u not in urls]
    if not urls:
        return ("No open-access location found.\nDOI: %s\nsources: %s\n"
                "It is probably paywalled — the author or an institutional "
                "subscription is the legitimate route." % (doi, "; ".join(notes)))

    tried = []
    for url in urls:
        ok, info = _download(url, path)
        tried.append(("OK  " if ok else "--  ") + url[:95] + "  ->  " + info)
        if ok:
            return ("Saved: %s (%s)\nDOI: %s\nsource: %s\nsources: %s\n"
                    "candidates tried: %d of %d" % (
                        path, info, doi, url[:110], "; ".join(notes),
                        len(tried), len(urls)))
    return ("Failed: none of %d candidate URLs returned a PDF for %s\n%s\n"
            "hint: get past the block with a browser session — export its "
            "cookies (context.storageState) and fetch with the same UA, or "
            "re-run with extra_urls pointing at a publisher mirror."
            % (len(urls), doi, "\n".join(tried)))
