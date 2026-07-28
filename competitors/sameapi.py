"""SameAPI export ingestion.

Upload a CSV export from sameapi.io (roughly once a month). We recognise the
export from its header, extract the visits_YYYY-MM-01 columns for rows with
status=Success, and upsert MonthlyTraffic (month x domain). Re-uploads
overwrite: the latest wins.
"""
import csv
import io
from datetime import date

from .models import Competitor, MonthlyTraffic, TrafficUpload

SAMEAPI_HEADER = "domain,status,title,global_rank"


def looks_like_sameapi(text):
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    return first_line.startswith(SAMEAPI_HEADER)


def parse_sameapi(text):
    """Parse an export → {(month: date, domain: str): visits: int}.

    Raises ValueError when the file is not a SameAPI export.
    """
    if not looks_like_sameapi(text):
        raise ValueError(
            'Questo file non sembra un export SameAPI (header atteso: '
            f'"{SAMEAPI_HEADER},…").')
    data = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        dom = (row.get("domain") or "").strip().lower()
        if not dom or row.get("status") != "Success":
            continue
        for col, v in row.items():
            if col and col.startswith("visits_") and v:
                ym = col[len("visits_"):][:7]  # YYYY-MM
                try:
                    y, m = int(ym[:4]), int(ym[5:7])
                    data[(date(y, m, 1), dom)] = int(float(v))
                except (ValueError, IndexError):
                    continue
    return data


def apply_upload(text, filename, uploaded_by=""):
    """Parse + upsert into MonthlyTraffic; record a TrafficUpload. Returns it."""
    data = parse_sameapi(text)
    comp_by_domain = {c.domain.lower(): c for c in Competitor.objects.exclude(domain="")}
    for (month, dom), visits in data.items():
        MonthlyTraffic.objects.update_or_create(
            month=month, domain=dom,
            defaults={"visits": visits, "competitor": comp_by_domain.get(dom)})
    months = sorted({m.isoformat()[:7] for m, _ in data})
    return TrafficUpload.objects.create(
        filename=filename[:300], uploaded_by=uploaded_by,
        months=months, domains=len({d for _, d in data}), datapoints=len(data))
