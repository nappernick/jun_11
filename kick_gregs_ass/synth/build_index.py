"""
Build a compact lookup table the Labeler agent uses to assign gold node IDs by
INTENT rather than keyword overlap.

The full corpus (data/faq_corpus.csv) carries long markdown bodies that are
expensive to skim repeatedly. The labeler only needs to know, per fragment:
what it is (title) and a short snippet to disambiguate near-duplicates. It opens
the full body from the corpus only when title+snippet are not enough.

Output: data/synthetic/corpus_index.tsv  (nodeId <TAB> title <TAB> snippet)
The snippet is the first ~200 chars of body with newlines flattened.
"""
import csv
import sys

import config

CORPUS_PATH = "data/faq_corpus.csv"
INDEX_PATH = "data/synthetic/corpus_index.tsv"
SNIPPET_CHARS = 200


def flatten(text, limit):
    """Collapse a markdown body to a single short line for the lookup table."""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def main(corpus_path=CORPUS_PATH, index_path=INDEX_PATH):
    rows_written = 0
    # The corpus has very large fields (full article bodies); raise the limit.
    csv.field_size_limit(sys.maxsize)
    with open(corpus_path, newline="", encoding="utf-8") as source, \
         open(index_path, "w", newline="", encoding="utf-8") as out:
        reader = csv.DictReader(source)
        writer = csv.writer(out, delimiter="\t")
        writer.writerow(["nodeId", "title", "snippet"])
        for row in reader:
            node_id = row[config.ID_COLUMN]
            title = (row.get("title") or "").strip()
            snippet = flatten(row.get(config.CONTENT_COLUMN, ""), SNIPPET_CHARS)
            writer.writerow([node_id, title, snippet])
            rows_written += 1
    print(f"Wrote {rows_written} corpus entries to {index_path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
