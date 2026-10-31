from typer.testing import CliRunner

from chapterwise.cli import app
from tests.conftest import words

runner = CliRunner()


def test_toc(sample_epub):
    result = runner.invoke(app, ["toc", str(sample_epub)])
    assert result.exit_code == 0, result.output
    assert "The Test Book — Ada Tester" in result.output
    assert "Chapter 2" in result.output


def test_text(sample_epub):
    result = runner.invoke(app, ["text", str(sample_epub), "2"])
    assert result.exit_code == 0
    assert "gamma0" in result.output


def test_dry_run_needs_no_api_key(sample_epub, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CHAPTERWISE_CACHE_DIR", str(tmp_path / "cache"))
    result = runner.invoke(app, ["summarize", str(sample_epub), "0-2", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Total: 3 call(s)" in result.output


def test_summarize_without_key_explains(sample_epub, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    result = runner.invoke(app, ["summarize", str(sample_epub), "0"])
    assert result.exit_code == 2
    assert "DEEPSEEK_API_KEY" in result.output


def test_bad_section_numbers(sample_epub):
    result = runner.invoke(app, ["summarize", str(sample_epub), "7", "--dry-run"])
    assert result.exit_code == 2
    assert "sections 0-2" in result.output
    result = runner.invoke(app, ["summarize", str(sample_epub), "x"])
    assert result.exit_code == 2


def test_unsupported_file(tmp_path):
    path = tmp_path / "book.docx"
    path.write_text(words("x"))
    result = runner.invoke(app, ["toc", str(path)])
    assert result.exit_code == 1
    assert "Unsupported format" in result.output
