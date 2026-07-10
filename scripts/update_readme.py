#!/usr/bin/env python3
"""Refresh the generated resource lists in README.md.

The script intentionally uses Python's standard library so it can run both
locally and in GitHub Actions without a dependency-install step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
HISTORY = ROOT / "data" / "resource-history.json"
ARXIV = "http://export.arxiv.org/api/query?"
GITHUB = "https://api.github.com/search/repositories?"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
USER_AGENT = "awesome-rag-medical-updater/1.0 (+https://github.com/usama-shiranai90/Awesome-RAG-Medical)"

TOPICS = {
    "Retrieval-Augmented Generation": ("all:\"retrieval augmented generation\"", ()),
    "RAG in healthcare": ("all:\"retrieval augmented generation\" AND all:medical", (("healthcare", "medical", "clinical", "health"),)),
    "RAG for clinical decision-making": ("all:\"retrieval augmented generation\" AND all:clinical", (("clinical", "decision", "diagnos", "treatment", "patient"),)),
    "RAG for medication and health": ("all:\"retrieval augmented generation\" AND all:drug AND all:health", (("medication", "drug", "pharmac", "prescri", "therap"), ("healthcare", "medical", "clinical", "health", "patient"))),
    "Agentic AI, RAG, and healthcare": ("all:\"retrieval augmented generation\" AND all:agent AND all:medical", (("agent",), ("healthcare", "medical", "clinical", "health", "patient"))),
}

PUBMED_QUERIES = {
    "RAG in healthcare": "(retrieval augmented generation[Title/Abstract] OR retrieval-augmented generation[Title/Abstract]) AND (healthcare[Title/Abstract] OR medical[Title/Abstract] OR clinical[Title/Abstract])",
    "RAG for clinical decision-making": "(retrieval augmented generation[Title/Abstract] OR retrieval-augmented generation[Title/Abstract]) AND (clinical decision[Title/Abstract] OR diagnosis[Title/Abstract])",
    "RAG for medication and health": "(retrieval augmented generation[Title/Abstract] OR retrieval-augmented generation[Title/Abstract]) AND (medication[Title/Abstract] OR drug[Title/Abstract] OR pharmacology[Title/Abstract])",
    "Agentic AI, RAG, and healthcare": "(agentic[Title/Abstract] OR AI agent[Title/Abstract]) AND (retrieval augmented generation[Title/Abstract] OR retrieval-augmented generation[Title/Abstract]) AND (healthcare[Title/Abstract] OR clinical[Title/Abstract])",
}


def get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_text(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def esc(value: str) -> str:
    return value.replace("|", "\\|")


def arxiv_papers(query: str, limit: int, required_groups: tuple[tuple[str, ...], ...]) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        # Fetch extra candidates because arXiv's full-text search can be broad;
        # title/abstract filtering below keeps topical tables trustworthy.
        "max_results": max(limit * 10, 30),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    root = ET.fromstring(get_text(ARXIV + params))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        title = clean(entry.findtext("atom:title", default="Untitled", namespaces=ns))
        published = entry.findtext("atom:published", default="", namespaces=ns)[:10]
        url = entry.findtext("atom:id", default="#", namespaces=ns)
        authors = [clean(author.findtext("atom:name", default="", namespaces=ns)) for author in entry.findall("atom:author", ns)]
        summary = clean(entry.findtext("atom:summary", default="", namespaces=ns)).lower()
        haystack = f"{title} {summary}".lower()
        has_rag = "retrieval-augmented generation" in haystack or "retrieval augmented generation" in haystack or " rag " in f" {haystack} "
        if has_rag and all(any(term in haystack for term in group) for group in required_groups):
            papers.append({"title": title, "date": published, "authors": ", ".join(authors[:2]) + (" et al." if len(authors) > 2 else ""), "url": url})
        if len(papers) == limit:
            break
    return papers


def github_repos(query: str, limit: int) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": limit})
    payload = get_json(GITHUB + params)
    return [
        {"name": item["full_name"], "description": clean(item.get("description") or "No description provided."), "url": item["html_url"], "updated": item["updated_at"][:10]}
        for item in payload.get("items", [])
    ]


def pubmed_articles(query: str, limit: int) -> list[dict[str, str]]:
    search_url = PUBMED_SEARCH + urllib.parse.urlencode({"db": "pubmed", "term": query, "retmax": limit, "sort": "pub date", "retmode": "json"})
    ids = get_json(search_url).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary_url = PUBMED_SUMMARY + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    result = get_json(summary_url).get("result", {})
    articles = []
    for pmid in ids:
        item = result.get(pmid, {})
        authors = item.get("authors", [])
        author_text = ", ".join(author.get("name", "") for author in authors[:2])
        if len(authors) > 2:
            author_text += " et al."
        articles.append({"title": clean(item.get("title", "Untitled")), "date": item.get("pubdate", "—"), "authors": author_text or "—", "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
    return articles


def table(rows: list[list[str]], headings: list[str]) -> str:
    output = ["| " + " | ".join(headings) + " |", "| " + " | ".join("---" for _ in headings) + " |"]
    output.extend("| " + " | ".join(esc(cell) for cell in row) + " |" for row in rows)
    return "\n".join(output)


def update_history(records: list[dict[str, str]]) -> None:
    """Merge this run into a durable, de-duplicated public resource archive."""
    existing = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    by_url = {item["url"]: item for item in existing}
    seen_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for record in records:
        item = by_url.get(record["url"])
        if item:
            item.update(record)
            item["last_seen"] = seen_at
        else:
            by_url[record["url"]] = {**record, "first_seen": seen_at, "last_seen": seen_at}
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(sorted(by_url.values(), key=lambda item: (item["type"], item["title"].lower())), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def generated_content(paper_limit: int, repo_limit: int) -> str:
    history_records: list[dict[str, str]] = []
    parts = ["<!-- GENERATED:START -->", "## Automatically refreshed resources", "", "This section is generated by [`scripts/update_readme.py`](scripts/update_readme.py). It is refreshed weekly; results are ranked by most recent submission or repository update."]
    for name, (query, required_groups) in TOPICS.items():
        parts += ["", f"### {name}", ""]
        try:
            papers = arxiv_papers(query, paper_limit, required_groups)
            history_records.extend({"topic": name, "type": "research paper", "title": paper["title"], "url": paper["url"], "published": paper["date"]} for paper in papers)
            rows = [[f"[{paper['title']}]({paper['url']})", paper["authors"] or "—", paper["date"] or "—"] for paper in papers]
            parts += ["Latest research papers (arXiv):", "", table(rows, ["Paper", "Authors", "Submitted"])] if rows else ["No recent arXiv results were returned."]
        except Exception as error:
            print(f"Warning: arXiv search for '{name}' failed: {error}", file=sys.stderr)
            parts += ["Latest research papers could not be fetched this run."]
        if name in PUBMED_QUERIES:
            try:
                articles = pubmed_articles(PUBMED_QUERIES[name], paper_limit)
                history_records.extend({"topic": name, "type": "clinical article", "title": article["title"], "url": article["url"], "published": article["date"]} for article in articles)
                if articles:
                    rows = [[f"[{article['title']}]({article['url']})", article["authors"], article["date"]] for article in articles]
                    parts += ["", "Latest clinical articles (PubMed):", "", table(rows, ["Article", "Authors", "Published"])]
            except Exception as error:
                print(f"Warning: PubMed search for '{name}' failed: {error}", file=sys.stderr)

    parts += ["", "### Datasets, tools, tutorials, and implementations", ""]
    searches = {
        "Datasets": "medical healthcare clinical dataset RAG",
        "Tools & implementations": "retrieval augmented generation healthcare language:Python",
        "Tutorials & examples": "RAG healthcare tutorial",
    }
    for label, query in searches.items():
        try:
            repos = github_repos(query, repo_limit)
            history_records.extend({"topic": label, "type": label.lower(), "title": repo["name"], "url": repo["url"], "published": repo["updated"]} for repo in repos)
            rows = [[f"[{repo['name']}]({repo['url']})", repo["description"], repo["updated"]] for repo in repos]
            parts += [f"#### {label}", "", table(rows, ["Resource", "Description", "Updated"]), ""] if rows else [f"#### {label}", "", "No GitHub results were returned.", ""]
        except Exception as error:
            print(f"Warning: GitHub search for '{label}' failed: {error}", file=sys.stderr)
            parts += [f"#### {label}", "", "Resources could not be fetched this run.", ""]
    update_history(history_records)
    parts += [f"Last generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. Every discovered item is retained in [`data/resource-history.json`](data/resource-history.json).", "<!-- GENERATED:END -->"]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the generated README resource lists.")
    parser.add_argument("--paper-limit", type=int, default=3, help="arXiv results per topic (default: 3)")
    parser.add_argument("--repo-limit", type=int, default=5, help="GitHub results per resource type (default: 5)")
    parser.add_argument("--check", action="store_true", help="exit 1 if README needs an update")
    args = parser.parse_args()
    content = README.read_text(encoding="utf-8")
    replacement = generated_content(args.paper_limit, args.repo_limit)
    pattern = r"<!-- GENERATED:START -->.*?<!-- GENERATED:END -->"
    if not re.search(pattern, content, flags=re.DOTALL):
        raise SystemExit("README.md is missing GENERATED markers")
    updated = re.sub(pattern, replacement, content, flags=re.DOTALL)
    if args.check:
        raise SystemExit(1 if updated != content else 0)
    README.write_text(updated, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
