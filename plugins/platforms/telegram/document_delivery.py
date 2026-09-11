"""Document metadata compatibility without changing Hermes' SendResult contract."""
from collections.abc import Mapping


def delivered_document_filename(result):
    legacy = getattr(result, "delivered_filename", None)
    if isinstance(legacy, str):
        return legacy
    raw = getattr(result, "raw_response", None)
    document = raw.get("document") if isinstance(raw, Mapping) else None
    name = document.get("file_name") if isinstance(document, Mapping) else None
    return name if isinstance(name, str) else None
