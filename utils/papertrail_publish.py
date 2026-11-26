"""Publish citecheck answers into a papertrail corpus. Run with papertrail's own Python:

    <papertrail>/.venv/bin/python -I papertrail_publish.py <corpus_root> <payloads.json>

payloads.json maps doc_id -> answer payload. Each payload is validated strictly with
papertrail's validate_and_enrich and stored as kind 'agent_adhoc' (listed in the web app's
answers sidebar). Prints {doc_id: {"id": ..., "warnings": [...]}} as the last line.
"""
import json
import sys

from papertrail.config import load_settings
from papertrail.core.citations import validate_and_enrich
from papertrail.core.registry import CorpusRegistry
from papertrail.core.store import Store


def main(corpus_root: str, payload_path: str, title_prefix: str = "Citation check") -> None:
    settings = load_settings(corpus_root)
    registry = CorpusRegistry(settings)
    store = Store(settings)
    payloads = json.load(open(payload_path))
    out = {}
    for doc_id, payload in payloads.items():
        enriched = validate_and_enrich(payload, registry.docs, registry.style, strict=True)
        title = f"{title_prefix}: {doc_id}"
        answer_id = store.create_answer("agent_adhoc", enriched, title=title)
        out[doc_id] = {"id": answer_id, "warnings": enriched.get("warnings", [])}
    print(json.dumps(out))


if __name__ == "__main__":
    main(*sys.argv[1:4])
