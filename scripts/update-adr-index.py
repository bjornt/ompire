#!/usr/bin/env python3
"""Regenerate the ADR table in docs/adr/README.md."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"
README = ADR_DIR / "README.md"
FILENAME_RE = re.compile(r"^(\d{4})-.+\.md$")
TITLE_RE = re.compile(r"^# ADR (\d{4}): (.+)$", re.MULTILINE)
STATUS_RE = re.compile(r"^- Status: (.+)$", re.MULTILINE)


def metadata(path: Path) -> tuple[str, str, str]:
    text = path.read_text()
    filename_match = FILENAME_RE.fullmatch(path.name)
    title_match = TITLE_RE.search(text)
    status_match = STATUS_RE.search(text)
    if not filename_match or not title_match or not status_match:
        raise ValueError(f"invalid ADR metadata: {path.relative_to(ROOT)}")

    number = filename_match.group(1)
    if title_match.group(1) != number:
        raise ValueError(f"ADR number does not match filename: {path.relative_to(ROOT)}")

    title = title_match.group(2).replace("|", r"\|")
    status = status_match.group(1).replace("|", r"\|")
    return number, title, status


def main() -> None:
    rows = []
    paths = sorted(path for path in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    for path in paths:
        number, title, status = metadata(path)
        rows.append(f"| [{number}]({path.name}) | {title} | {status} |")

    table = "\n".join(
        [
            "| ADR | Decision | Status |",
            "|---|---|---|",
            *rows,
        ]
    )

    readme = README.read_text()
    index_start = readme.index("## Index\n") + len("## Index\n")
    index_end = readme.index("\n## ", index_start)
    README.write_text(f"{readme[:index_start]}\n{table}\n{readme[index_end:]}")


if __name__ == "__main__":
    main()
