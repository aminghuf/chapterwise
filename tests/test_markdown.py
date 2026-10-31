from chapterwise.parsers import parse
from tests.conftest import words


def write(tmp_path, content, name="playbook.md"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_single_h1_is_title_and_h2_are_sections(tmp_path):
    md = f"""# Kubernetes Playbook

{words("intro", 25)}

## Setup

{words("setup")}

```bash
# not a heading
kubectl get pods
```

### Detail

{words("detail")}

## Operate

{words("operate")}
"""
    doc = parse(write(tmp_path, md))
    assert doc.title == "Kubernetes Playbook"
    assert [s.title for s in doc.sections] == ["Introduction", "Setup", "Operate"]
    setup = doc.sections[1].text
    assert "detail0" in setup  # subsections stay inside their section
    assert "# not a heading" in setup  # code blocks are kept, not split on


def test_frontmatter(tmp_path):
    md = f"""---
title: "From Front Matter"
author: Amin
---
## One
{words("one")}
## Two
{words("two")}
## Three
{words("three")}
"""
    doc = parse(write(tmp_path, md))
    assert doc.title == "From Front Matter"
    assert doc.author == "Amin"
    assert [s.title for s in doc.sections] == ["One", "Two", "Three"]


def test_no_headings_is_one_section(tmp_path):
    doc = parse(write(tmp_path, words("plain"), name="my-notes.md"))
    assert doc.title == "my notes"
    assert len(doc.sections) == 1


def test_code_indentation_preserved(tmp_path):
    md = f"## Code\n\n{words('x')}\n\n```python\ndef f():\n    return 1\n```\n"
    doc = parse(write(tmp_path, md))
    assert "    return 1" in doc.sections[0].text
