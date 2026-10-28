"""Historical document corpus used by the Contextual Reasoning Agent."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import KNOWLEDGE_DIR


@dataclass(slots=True)
class Document:
    doc_id: str
    title: str
    body: str
    tags: set[str]

    @property
    def words(self) -> int:
        return len(self.body.split())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_corpus(directory: Path = KNOWLEDGE_DIR) -> list[Document]:
    documents: list[Document] = []
    if not directory.exists():
        return documents
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s*(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        tags = {_slug(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", text.lower())}
        documents.append(Document(doc_id=path.stem, title=title, body=text, tags=tags))
    return documents


def retrieve(query: str, corpus: list[Document], *, max_chars: int = 12_000) -> list[Document]:
    """Pick the corpus slices most relevant to a defect, ordered by term overlap."""
    terms = {_slug(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", query.lower())}
    scored = []
    for document in corpus:
        overlap = len(terms & document.tags)
        if overlap:
            scored.append((overlap, document))
    scored.sort(key=lambda pair: -pair[0])

    selected: list[Document] = []
    budget = max_chars
    for _, document in scored:
        if document.words * 6 > budget:
            continue
        selected.append(document)
        budget -= document.words * 6
        if budget <= 0:
            break
    return selected


def format_context(documents: list[Document]) -> str:
    return "\n\n".join(f"### [{d.doc_id}] {d.title}\n{d.body}" for d in documents)
