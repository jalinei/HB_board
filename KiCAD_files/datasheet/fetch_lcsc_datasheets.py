#!/usr/bin/env python3
"""Download LCSC datasheets for relevant non-standard parts in a KiCad .net file.

Default policy:
  - include ICs and non-standard parts: U, D, F, J, Q, L, T, K, Y, SW
  - exclude ordinary resistors/capacitors: R, C
  - de-duplicate by LCSC code

The script starts from:
    https://www.lcsc.com/datasheet/Cxxxxxx.pdf
LCSC currently serves an HTML wrapper at that URL for many parts, so the script
also follows the embedded datasheet iframe to retrieve the actual PDF.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_PREFIXES = ("U", "D", "F", "J", "Q", "L", "T", "K", "Y", "SW")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 LCSC-datasheet-fetcher/1.0"


def balanced_comp_blocks(text: str):
    """Yield balanced '(comp ...)' blocks from a KiCad legacy/netlist S-expression."""
    for m in re.finditer(r"\(comp(?=\s)", text):
        start = m.start()
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break


def first_match(pattern: str, text: str) -> str:
    m = re.search(pattern, text, flags=re.S)
    return m.group(1).strip() if m else ""


def parse_parts(netlist: Path, prefixes=DEFAULT_PREFIXES):
    text = netlist.read_text(encoding="utf-8", errors="replace")
    parts = []

    for block in balanced_comp_blocks(text):
        ref = first_match(r'\(ref\s+"([^"]+)"\)', block)
        value = first_match(r'\(value\s+"([^"]*)"\)', block)
        footprint = first_match(r'\(footprint\s+"([^"]*)"\)', block)

        lcsc = first_match(
            r'\(field\s+\(name\s+"LCSC"\)\s+"([^"]*)"\)', block
        )
        if not lcsc:
            lcsc = first_match(
                r'\(property\s+\(name\s+"LCSC"\)\s+\(value\s+"([^"]*)"\)',
                block,
            )

        if not lcsc:
            continue

        # Normalize common accidental whitespace and validate LCSC code.
        lcsc = lcsc.strip().upper()
        if not re.fullmatch(r"C\d+", lcsc):
            print(f"WARNING: ignoring malformed LCSC field {lcsc!r} on {ref}", file=sys.stderr)
            continue

        if not any(ref.startswith(prefix) for prefix in prefixes):
            continue

        parts.append(
            {
                "ref": ref,
                "value": value,
                "footprint": footprint,
                "lcsc": lcsc,
            }
        )

    return parts


def safe_name(s: str) -> str:
    s = s.strip()
    if not s or s == "~":
        return "part"
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", s)
    return s.strip("_.") or "part"


def http_get(url: str, timeout: float = 30.0):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as response:
        data = response.read()
        ctype = response.headers.get("Content-Type", "")
        final_url = response.geturl()
    return data, ctype, final_url


def extract_pdf_url(page: bytes, base_url: str) -> str | None:
    """Extract the actual LCSC datasheet PDF URL from the HTML wrapper.

    Important: LCSC also embeds Google Tag Manager in an iframe.  Do not take
    the first iframe blindly; only accept links that point to a PDF or to the
    datasheet.lcsc.com host.
    """
    text = page.decode("utf-8", errors="replace")

    # 1) Prefer an explicit absolute datasheet.lcsc.com PDF URL anywhere in
    #    the page/source. This is the most reliable form used by LCSC today.
    m = re.search(
        r'https?:\/\/datasheet\.lcsc\.com\/[^"\'<>\s]+?\.pdf(?:\?[^"\'<>\s]*)?',
        text,
        flags=re.I,
    )
    if m:
        return html.unescape(m.group(0)).replace("\\/", "/")

    # 2) Inspect every iframe src/data-src, but keep only datasheet/PDF URLs.
    for m in re.finditer(
        r'<iframe[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
        text,
        flags=re.I,
    ):
        candidate = html.unescape(m.group(1)).replace("\\/", "/")
        absolute = urljoin(base_url, candidate)
        if "datasheet.lcsc.com" in absolute.lower() or re.search(r'\.pdf(?:$|[?#])', absolute, re.I):
            return absolute

    # 3) Last-resort: any href/src containing a PDF, while explicitly rejecting
    #    analytics/tag-manager URLs.
    for m in re.finditer(
        r'(?:href|src|data-src)=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        text,
        flags=re.I,
    ):
        candidate = html.unescape(m.group(1)).replace("\\/", "/")
        absolute = urljoin(base_url, candidate)
        if "googletagmanager.com" not in absolute.lower():
            return absolute

    return None


def download_pdf(lcsc: str, output: Path, timeout: float = 30.0):
    landing_url = f"https://www.lcsc.com/datasheet/{lcsc}.pdf"
    data, ctype, final_url = http_get(landing_url, timeout=timeout)

    if data.startswith(b"%PDF") or "application/pdf" in ctype.lower():
        pdf = data
        pdf_url = final_url
    else:
        pdf_url = extract_pdf_url(data, final_url)
        if not pdf_url:
            raise RuntimeError("LCSC page did not expose a PDF iframe/link")
        pdf, pdf_ctype, _ = http_get(pdf_url, timeout=timeout)
        if not pdf.startswith(b"%PDF") and "application/pdf" not in pdf_ctype.lower():
            raise RuntimeError(f"resolved URL did not return a PDF: {pdf_url}")

    output.write_bytes(pdf)
    return landing_url, pdf_url, len(pdf)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", type=Path, help="KiCad .net file")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("datasheets"))
    parser.add_argument("--dry-run", action="store_true", help="list selected parts without downloading")
    parser.add_argument("--delay", type=float, default=0.25, help="delay between downloads in seconds")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    parts = parse_parts(args.netlist)
    grouped = defaultdict(list)
    for part in parts:
        grouped[part["lcsc"]].append(part)

    if not grouped:
        print("No relevant components with valid LCSC references found.")
        return 0

    print(f"Found {len(parts)} placed relevant parts, {len(grouped)} unique LCSC datasheets.\n")

    for lcsc, items in sorted(grouped.items()):
        refs = ",".join(item["ref"] for item in items)
        values = sorted({item["value"] for item in items if item["value"] and item["value"] != "~"})
        label = values[0] if values else items[0]["footprint"].split(":")[-1]
        print(f"{lcsc:>10}  {label:<36}  [{refs}]")

    if args.dry_run:
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    print()
    for lcsc, items in sorted(grouped.items()):
        values = sorted({item["value"] for item in items if item["value"] and item["value"] != "~"})
        label = values[0] if values else items[0]["footprint"].split(":")[-1]
        filename = f"{lcsc}_{safe_name(label)}.pdf"
        output = args.output_dir / filename

        if output.exists() and output.stat().st_size > 1000:
            print(f"SKIP  {lcsc}: {output} already exists")
            continue

        try:
            landing_url, pdf_url, size = download_pdf(lcsc, output, timeout=args.timeout)
            print(f"OK    {lcsc}: {output}  ({size / 1024:.1f} KiB)")
            if pdf_url != landing_url:
                print(f"      resolved: {pdf_url}")
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
            failures += 1
            print(f"FAIL  {lcsc}: {exc}", file=sys.stderr)

        if args.delay > 0:
            time.sleep(args.delay)

    if failures:
        print(f"\nCompleted with {failures} failed datasheet(s).", file=sys.stderr)
        return 1

    print("\nAll selected datasheets downloaded successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
