#!/usr/bin/env python3
"""Refresh the generated resource lists in README.md.

The script intentionally uses Python's standard library so it can run both
locally and in GitHub Actions without a dependency-install step.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
HISTORY = ROOT / "data" / "resource-history.json"
CATALOG = ROOT / "catalog"
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
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    request = urllib.request.Request(url, headers=headers)
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


def arxiv_papers(query: str, limit: int | None, required_groups: tuple[tuple[str, ...], ...]) -> list[dict[str, str]]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    search_size = 500 if limit is None else max(limit * 10, 30)
    papers = []
    start = 0
    while True:
        params = urllib.parse.urlencode({
            "search_query": query,
            "start": start,
            "max_results": search_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        root = ET.fromstring(get_text(ARXIV + params))
        entries = root.findall("atom:entry", ns)
        for entry in entries:
            title = clean(entry.findtext("atom:title", default="Untitled", namespaces=ns))
            published = entry.findtext("atom:published", default="", namespaces=ns)[:10]
            url = re.sub(r"v\d+$", "", entry.findtext("atom:id", default="#", namespaces=ns))
            authors = [clean(author.findtext("atom:name", default="", namespaces=ns)) for author in entry.findall("atom:author", ns)]
            summary = clean(entry.findtext("atom:summary", default="", namespaces=ns)).lower()
            haystack = f"{title} {summary}".lower()
            has_rag = "retrieval-augmented generation" in haystack or "retrieval augmented generation" in haystack or " rag " in f" {haystack} "
            if has_rag and all(any(term in haystack for term in group) for group in required_groups):
                papers.append({"title": title, "date": published, "authors": ", ".join(authors[:2]) + (" et al." if len(authors) > 2 else ""), "url": url})
            if limit is not None and len(papers) == limit:
                break
        if limit is not None or not entries:
            break
        start += len(entries)
        total = int(root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults", default="0"))
        if start >= total:
            break
        time.sleep(3)
    return papers


def github_repos(query: str, limit: int) -> list[dict[str, str]]:
    repos = []
    for page in range(1, (min(limit, 1000) + 99) // 100 + 1):
        params = urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": min(100, limit), "page": page})
        items = get_json(GITHUB + params).get("items", [])
        repos.extend({"name": item["full_name"], "description": clean(item.get("description") or "No description provided."), "url": item["html_url"], "updated": item["updated_at"][:10]} for item in items)
        if len(items) < min(100, limit) or len(repos) >= limit:
            break
    return repos[:limit]


def pubmed_articles(query: str, limit: int | None) -> list[dict[str, str]]:
    search_url = PUBMED_SEARCH + urllib.parse.urlencode({"db": "pubmed", "term": query, "retmax": limit or 10000, "sort": "pub date", "retmode": "json"})
    ids = get_json(search_url).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    articles = []
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset + 200]
        summary_url = PUBMED_SUMMARY + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(batch), "retmode": "json"})
        result = get_json(summary_url).get("result", {})
        for pmid in batch:
            item = result.get(pmid, {})
            authors = item.get("authors", [])
            author_text = ", ".join(author.get("name", "") for author in authors[:2])
            if len(authors) > 2:
                author_text += " et al."
            articles.append({"title": clean(item.get("title", "Untitled")), "date": item.get("pubdate", "—"), "authors": author_text or "—", "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
        if offset + 200 < len(ids):
            time.sleep(0.35)
    return articles


def table(rows: list[list[str]], headings: list[str]) -> str:
    output = ["| " + " | ".join(headings) + " |", "| " + " | ".join("---" for _ in headings) + " |"]
    output.extend("| " + " | ".join(esc(cell) for cell in row) + " |" for row in rows)
    return "\n".join(output)


def update_history(records: list[dict[str, str]]) -> list[dict]:
    """Merge this run into a durable, de-duplicated public resource archive."""
    existing = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    by_url = {}
    for item in existing:
        if "topics" not in item:
            item["topics"] = [item.pop("topic")]
        item["url"] = re.sub(r"v\d+$", "", item["url"])
        by_url[item["url"]] = item
    seen_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for record in records:
        item = by_url.get(record["url"])
        if item:
            item["topics"] = sorted(set(item["topics"] + [record.pop("topic")]))
            item.update(record)
            item["last_seen"] = seen_at
        else:
            topic = record.pop("topic")
            by_url[record["url"]] = {**record, "topics": [topic], "first_seen": seen_at, "last_seen": seen_at}
    HISTORY.parent.mkdir(exist_ok=True)
    history = sorted(by_url.values(), key=lambda item: (item["type"], item.get("published", ""), item["title"].lower()), reverse=True)
    HISTORY.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return history


def write_catalog(history: list[dict], page_size: int = 300) -> list[dict]:
    """Write the durable archive as GitHub-renderable Markdown pages."""
    labels = {
        "research paper": ("Research papers", "research-papers"),
        "clinical article": ("Clinical articles and findings", "clinical-articles"),
        "datasets": ("Datasets", "datasets"),
        "tools & implementations": ("Tools and implementations", "tools-and-implementations"),
        "tutorials & examples": ("Tutorials and examples", "tutorials-and-examples"),
    }
    CATALOG.mkdir(exist_ok=True)
    index = []
    for resource_type, (label, slug) in labels.items():
        items = [item for item in history if item["type"] == resource_type]
        if not items:
            continue
        pages = []
        for page_number, offset in enumerate(range(0, len(items), page_size), start=1):
            page_items = items[offset:offset + page_size]
            filename = f"{slug}-{page_number:02d}.md"
            rows = [[
                f"[{item['title']}]({item['url']})",
                ", ".join(item["topics"]),
                item.get("details", "—"),
                item.get("published", "—"),
            ] for item in page_items]
            content = "\n".join([
                f"# {label} — page {page_number}",
                "",
                f"Generated catalogue entries {offset + 1}–{offset + len(page_items)} of {len(items)}.",
                "",
                table(rows, ["Resource", "Topics", "Authors / description", "Published / updated"]),
                "",
                "[Back to the main catalogue](../README.md#complete-historical-catalogue)",
                "",
            ])
            (CATALOG / filename).write_text(content, encoding="utf-8", newline="\n")
            pages.append(filename)
        index.append({"label": label, "count": len(items), "pages": pages})
    return index


def generated_content(paper_limit: int, repo_limit: int, fetch_all: bool = False) -> str:
    history_records: list[dict[str, str]] = []
    parts = ["<!-- GENERATED:START -->", "## Automatically refreshed resources", "", "This section is generated by [`scripts/update_readme.py`](scripts/update_readme.py). It is refreshed weekly; results are ranked by most recent submission or repository update."]
    for name, (query, required_groups) in TOPICS.items():
        parts += ["", f"### {name}", ""]
        try:
            papers = arxiv_papers(query, None if fetch_all else paper_limit, required_groups)
            print(f"arXiv / {name}: {len(papers)}")
            history_records.extend({"topic": name, "type": "research paper", "title": paper["title"], "url": paper["url"], "published": paper["date"], "details": paper["authors"] or "—"} for paper in papers)
            rows = [[f"[{paper['title']}]({paper['url']})", paper["authors"] or "—", paper["date"] or "—"] for paper in papers[:paper_limit]]
            parts += ["Latest research papers (arXiv):", "", table(rows, ["Paper", "Authors", "Submitted"])] if rows else ["No recent arXiv results were returned."]
        except Exception as error:
            print(f"Warning: arXiv search for '{name}' failed: {error}", file=sys.stderr)
            parts += ["Latest research papers could not be fetched this run."]
        if name in PUBMED_QUERIES:
            try:
                articles = pubmed_articles(PUBMED_QUERIES[name], None if fetch_all else paper_limit)
                print(f"PubMed / {name}: {len(articles)}")
                history_records.extend({"topic": name, "type": "clinical article", "title": article["title"], "url": article["url"], "published": article["date"], "details": article["authors"]} for article in articles)
                if articles:
                    rows = [[f"[{article['title']}]({article['url']})", article["authors"], article["date"]] for article in articles[:paper_limit]]
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
            repos = github_repos(query, 1000 if fetch_all else repo_limit)
            print(f"GitHub / {label}: {len(repos)}")
            history_records.extend({"topic": label, "type": label.lower(), "title": repo["name"], "url": repo["url"], "published": repo["updated"], "details": repo["description"]} for repo in repos)
            rows = [[f"[{repo['name']}]({repo['url']})", repo["description"], repo["updated"]] for repo in repos[:repo_limit]]
            parts += [f"#### {label}", "", table(rows, ["Resource", "Description", "Updated"]), ""] if rows else [f"#### {label}", "", "No GitHub results were returned.", ""]
        except Exception as error:
            print(f"Warning: GitHub search for '{label}' failed: {error}", file=sys.stderr)
            parts += [f"#### {label}", "", "Resources could not be fetched this run.", ""]
    history = update_history(history_records)
    catalog_index = write_catalog(history)
    parts += ["", "### Complete historical catalogue", "", "All resources discovered so far are listed below and retained across weekly refreshes.", ""]
    rows = []
    for section in catalog_index:
        links = ", ".join(f"[Part {number}](catalog/{filename})" for number, filename in enumerate(section["pages"], start=1))
        rows.append([section["label"], str(section["count"]), links])
    parts += [table(rows, ["Category", "Resources", "Catalogue pages"]), ""]
    parts += [f"Last generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. Every discovered item is retained in [`data/resource-history.json`](data/resource-history.json).", "<!-- GENERATED:END -->"]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the generated README resource lists.")
    parser.add_argument("--paper-limit", type=int, default=3, help="arXiv results per topic (default: 3)")
    parser.add_argument("--repo-limit", type=int, default=5, help="GitHub results per resource type (default: 5)")
    parser.add_argument("--all", action="store_true", help="backfill all matching results exposed by the public APIs")
    parser.add_argument("--check", action="store_true", help="exit 1 if README needs an update")
    args = parser.parse_args()
    content = README.read_text(encoding="utf-8")
    replacement = generated_content(args.paper_limit, args.repo_limit, args.all)
    pattern = r"<!-- GENERATED:START -->.*?<!-- GENERATED:END -->"
    if not re.search(pattern, content, flags=re.DOTALL):
        raise SystemExit("README.md is missing GENERATED markers")
    updated = re.sub(pattern, replacement, content, flags=re.DOTALL)
    if args.check:
        raise SystemExit(1 if updated != content else 0)
    README.write_text(updated, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
