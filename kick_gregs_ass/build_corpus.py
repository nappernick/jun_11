"""
Build data/faq_corpus.csv for the retrieval backend from the two source artifacts:

  - data/results.jsonl     : the scraped article BODIES (markdown) + nodeId/title/
                             status/geography. The source of truth for content.
  - data/source_export.csv : the original CoreX export. Metadata + a link only; no
                             body. We pull the targeting fields (Job Level /
                             Location Type / Employee Class / Line Of Business)
                             from here, joined on nodeId.

Output schema matches config.py exactly:
  ID_COLUMN      = "nodeId"
  CONTENT_COLUMN = "text"   (must be the LAST column)
  filter fields  = system_job-level / system_location-type /
                   system_employee-class / system_line-of-business / geography
Multi-value cells are pipe-delimited (config.MULTIVALUE_DELIMITER = "|"), so we
convert the export's comma-separated lists to pipes.

Any targeting field missing for a node is synthesized to that field's wildcard
("applies to everyone") token, which is the realistic default in production.
"""
import csv
import json

JSONL_PATH = "data/results.jsonl"        # source: scraped article bodies
EXPORT_PATH = "data/source_export.csv"   # source: raw CoreX metadata export
OUT_PATH = "data/faq_corpus.csv"         # generated corpus the backend ingests

# CoreX export column -> backend filter field name. Wildcard is the synthesized
# default when the export has no value for that node.
EXPORT_TO_FIELD = {
    "Job Level":        ("system_job-level",        "All Job Levels"),
    "Location Type":    ("system_location-type",    "All Location Types"),
    "Employee Class":   ("system_employee-class",   "All Employee Classes"),
    "Line Of Business": ("system_line-of-business",  "All LOBs"),
    "Geography":        ("geography",                "Global"),
}

OUT_COLUMNS = [
    "nodeId", "title", "version", "status",
    "system_job-level", "system_location-type",
    "system_employee-class", "system_line-of-business", "geography",
    "contentUrl",
    "text",   # CONTENT_COLUMN — must stay last
]


def commas_to_pipes(value):
    """Export multi-value cells use commas; backend splits on '|'."""
    if value is None:
        return ""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return "|".join(parts)


def load_export_meta(path):
    """nodeId -> targeting metadata pulled from the CoreX export."""
    meta = {}
    # The export has one stray non-UTF-8 byte; replace it rather than fail.
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            node_id = row["Node Id"]
            entry = {}
            for export_col, (field, _wildcard) in EXPORT_TO_FIELD.items():
                entry[field] = commas_to_pipes(row.get(export_col))
            meta[node_id] = entry
    return meta


def main():
    export_meta = load_export_meta(EXPORT_PATH)

    seen = set()
    out_rows = []
    with open(JSONL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            node_id = rec["nodeId"]
            if node_id in seen:
                continue  # dedupe repeated nodeIds (keep first)
            seen.add(node_id)

            meta = export_meta.get(node_id, {})
            row = {
                "nodeId": node_id,
                "title": rec.get("title", ""),
                "version": rec.get("version", ""),
                "status": rec.get("status", ""),
                "contentUrl": rec.get("contentUrl", ""),
                "text": rec.get("markdown", ""),
            }
            for _export_col, (field, wildcard) in EXPORT_TO_FIELD.items():
                row[field] = meta.get(field) or wildcard
            out_rows.append(row)

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Wrote {len(out_rows)} fragments to {OUT_PATH}")
    missing = [r["nodeId"] for r in out_rows if r["nodeId"] not in export_meta]
    if missing:
        print(f"  {len(missing)} node(s) had no export metadata; "
              f"targeting fields synthesized to wildcards.")


if __name__ == "__main__":
    main()
