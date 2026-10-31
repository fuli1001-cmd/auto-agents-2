"""The research stage publishes a complete replacement, not file changes."""
from .errors import InvalidStructuredResponse

RESEARCH_PUBLICATION_RULE = (
    "Research publication is a complete replacement: only document is written to "
    "docs/aa2/provider_research.md, and the current sources set is retained for downstream stages. "
    "The document and source set must be independently consumable and cover the complete research scope, "
    "including still-valid information when correcting earlier claims. files must be empty: no companion "
    "file is applied. Previous proposals and reviews are correction references, not automatically inherited "
    "publication or approval. Do not silently retain rejected claims, merge sources by count, or approve an "
    "incremental note on the assumption that previous content or unapplied files will be published."
)


def research_output_contract():
    return {"version": 1, "publication_path": "docs/aa2/provider_research.md",
            "mode": "complete_replacement", "complete_fields": ["document", "sources"],
            "files": "must_be_empty", "previous": "historical_reference_only"}


def validate_research_output(document):
    if document.get("files"):
        raise InvalidStructuredResponse(
            "provider_research.files cannot be published by this stage. Preserve the proposal and return "
            "the complete standalone report in document with its complete sources set and files=[]. "
            "Use prior material as correction evidence, not an implicit inherited publication.")
