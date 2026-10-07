"""Copy docs/documentation into a clone of the GitHub wiki, so the two can't drift.

    git clone git@github.com:BenCurrie42/hackathon_2026.wiki.git /tmp/wiki
    uv run python scripts/publish_wiki.py /tmp/wiki
    cd /tmp/wiki && git add -A && git commit -m "..." && git push

The repo pages are the source. Each becomes its wiki page (README.md becomes
Home), links between pages become wiki links, links to other repo files become
GitHub links on main, and _Sidebar.md is rebuilt from the README's tables.
Wiki pages with no repo page are left alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "documentation"
BLOB = "https://github.com/BenCurrie42/hackathon_2026/blob/main/"

# repo file (relative to the repo root) -> wiki page name
PAGES = {
    "docs/documentation/README.md": "Home",
    "docs/documentation/getting-started.md": "Getting-Started",
    "docs/documentation/weekly-workflow.md": "Weekly-Workflow",
    "docs/documentation/cli-and-live-control.md": "CLI-and-Live-Control",
    "docs/documentation/troubleshooting.md": "Troubleshooting",
    "docs/documentation/architecture.md": "Architecture",
    "docs/documentation/part-tracks-and-imports.md": "Part-Tracks-And-Imports",
    "docs/documentation/file-renderer.md": "File-Renderer",
    "docs/documentation/ableton-format-notes.md": "Ableton-Format-Notes",
    "docs/documentation/riglink.md": "RigLink",
    "docs/documentation/web-server-api.md": "Web-Server-API",
    "docs/documentation/assistant-and-actions.md": "Assistant-And-Actions",
    "docs/documentation/audio-analysis-and-memory.md": "Audio-Analysis-And-Memory",
    "docs/documentation/frontend.md": "Frontend",
    "docs/documentation/testing-and-development.md": "Testing-And-Development",
    "docs/agents_of_flourishing.md": "Agents-Of-Flourishing",
}

_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)#\s]+)(#[^)\s]*)?\)")
_LINE_NOTE = "This folder is mirrored to the GitHub wiki"


def wiki_text(source, text):
    """A repo page's text with its links pointed at wiki pages and GitHub files."""
    here = (ROOT / source).parent

    def relink(match):
        target, anchor = match.group(1), match.group(2) or ""
        path = (here / target).resolve()
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            return match.group(0)
        if rel in PAGES:
            return f"]({PAGES[rel]}{anchor})"
        return f"]({BLOB}{rel}{anchor})"

    lines = [line for line in text.splitlines() if _LINE_NOTE not in line]
    return re.sub(r"\n{3,}", "\n\n", _LINK.sub(relink, "\n".join(lines))).strip() + "\n"


def sidebar(home):
    """The sidebar: Home, then each README section's pages in order."""
    out = ["**[Home](Home)**", ""]
    for line in home.splitlines():
        if line.startswith("## "):
            out += [f"**{line[3:].strip()}**", ""]
        page = re.match(r"\| \[([^\]]+)\]\(([^)]+)\)", line)
        if page:
            out.append(f"- [{page.group(1)}]({page.group(2)})")
        elif not line.strip() and out[-1].startswith("- "):
            out.append("")
    return "\n".join(out).strip() + "\n"


def main(argv):
    if len(argv) != 2 or not (Path(argv[1]) / ".git").exists():
        sys.exit("Give the path of a clone of the wiki: scripts/publish_wiki.py /path/to/hackathon_2026.wiki")
    wiki = Path(argv[1])
    for source, page in PAGES.items():
        text = wiki_text(source, (ROOT / source).read_text())
        (wiki / f"{page}.md").write_text(text)
        if page == "Home":
            (wiki / "_Sidebar.md").write_text(sidebar(text))
    print(f"Wrote {len(PAGES)} pages and the sidebar to {wiki}.")


if __name__ == "__main__":
    main(sys.argv)
