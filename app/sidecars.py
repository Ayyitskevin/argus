"""Sidecar export helpers for Argus.

These functions are intentionally framework-free so the API server, CLI, and
HTTP client can share the same JSON/IPTC/XMP behavior without importing
FastAPI or starting app-level workers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _xml_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_xmp(analysis_data: dict) -> str:
    """Build a small Lightroom/Capture One-compatible XMP sidecar."""
    iptc = analysis_data.get("suggested_iptc") or {}
    headline = iptc.get("headline", "")
    caption = iptc.get("caption", "")
    keywords = iptc.get("keywords") or analysis_data.get("keywords") or []
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    kw_xml = "\n".join(
        f"        <rdf:li>{_xml_escape(keyword)}</rdf:li>"
        for keyword in keywords
    )

    return f'''<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d">
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Argus">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
 <rdf:Description rdf:about=""
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
  <dc:title>
   <rdf:Alt>
    <rdf:li xml:lang="x-default">{_xml_escape(headline)}</rdf:li>
   </rdf:Alt>
  </dc:title>
  <dc:description>
   <rdf:Alt>
    <rdf:li xml:lang="x-default">{_xml_escape(caption)}</rdf:li>
   </rdf:Alt>
  </dc:description>
  <dc:subject>
   <rdf:Bag>
{kw_xml}
   </rdf:Bag>
  </dc:subject>
  <Iptc4xmpCore:Headline>{_xml_escape(headline)}</Iptc4xmpCore:Headline>
  <Iptc4xmpCore:Caption>{_xml_escape(caption)}</Iptc4xmpCore:Caption>
 </rdf:Description>
</rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>'''


def write_sidecar(
    image_path: str | Path,
    analysis_data: dict,
    sidecar_dir: str | Path | None = None,
) -> dict[str, Path]:
    """Write Argus JSON, IPTC JSON, and XMP sidecars without touching originals."""
    source = Path(image_path)
    out_dir = Path(sidecar_dir) if sidecar_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    base = source.stem
    argus_path = out_dir / f"{base}.argus.json"
    argus_path.write_text(
        json.dumps(analysis_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    written = {"argus": argus_path}

    iptc = analysis_data.get("suggested_iptc") or {}
    if iptc:
        iptc_path = out_dir / f"{base}.iptc.json"
        iptc_path.write_text(
            json.dumps(iptc, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written["iptc"] = iptc_path

        xmp_path = out_dir / f"{base}.xmp"
        xmp_path.write_text(build_xmp(analysis_data), encoding="utf-8")
        written["xmp"] = xmp_path

    return written
