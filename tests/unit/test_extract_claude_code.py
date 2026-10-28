import json
import os
import stat
import sys
from pathlib import Path

import pytest

from crate2discogs.extract.base import ExtractionFailed, ExtractorUnavailable
from crate2discogs.extract.claude_code import ClaudeCodeExtractor

VALID = {"photo_type": "shop_sticker", "catno": "DKMNTL 031", "title": "PARADYGM SHIFT (2LP)"}

FAKE = """#!{python}
import json, os, sys, time
from pathlib import Path
mode = os.environ["FAKE_CLAUDE_MODE"]
calls = Path(os.environ["FAKE_CLAUDE_CALLS"])
n = int(calls.read_text()) + 1 if calls.exists() else 1
calls.write_text(str(n))
Path(os.environ["FAKE_CLAUDE_ARGS"]).write_text(json.dumps(sys.argv[1:]))
valid = {valid!r}
if mode == "structured":
    print(json.dumps({{"type": "result", "is_error": False, "result": "", "structured_output": valid}}))
elif mode == "text":
    fenced = "```json\\n" + json.dumps(valid) + "\\n```"
    print(json.dumps({{"type": "result", "is_error": False, "result": fenced}}))
elif mode == "garbage":
    print("not json at all")
elif mode == "garbage_then_valid":
    print("garbage" if n == 1 else json.dumps({{"result": "", "structured_output": valid}}))
elif mode == "fail":
    print("boom: not logged in", file=sys.stderr)
    sys.exit(3)
elif mode == "slow":
    time.sleep(5)
"""


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE.format(python=sys.executable, valid=VALID))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def image(tmp_path: Path) -> Path:
    d = tmp_path / "photos"
    d.mkdir()
    p = d / "x.jpg"
    p.write_bytes(b"jpeg")
    return p


def _calls(fake: Path) -> int:
    return int((fake / "calls").read_text())


def test_structured_output_parsed(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "structured")
    e = ClaudeCodeExtractor().extract(image).extraction
    assert (e.catno, e.title, e.disc_count) == ("DKMNTL 031", "PARADYGM SHIFT", 2)
    args = json.loads((fake / "args.json").read_text())
    assert args[0] == "-p"
    assert str(image) in args[1]
    assert args[args.index("--output-format") + 1] == "json"
    assert json.loads(args[args.index("--json-schema") + 1])["type"] == "object"
    assert args[args.index("--add-dir") + 1] == str(image.parent)
    assert "--bare" not in args  # bare mode ignores the subscription login


def test_result_text_fallback(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "text")
    assert ClaudeCodeExtractor().extract(image).extraction.catno == "DKMNTL 031"


def test_garbage_retry_then_fail(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    with pytest.raises(ExtractionFailed, match="invalid output after retry"):
        ClaudeCodeExtractor().extract(image)
    assert _calls(fake) == 2


def test_garbage_then_valid(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage_then_valid")
    assert ClaudeCodeExtractor().extract(image).extraction.catno == "DKMNTL 031"
    assert _calls(fake) == 2


def test_nonzero_exit_captures_stderr(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    with pytest.raises(ExtractionFailed, match="exited 3: boom: not logged in"):
        ClaudeCodeExtractor().extract(image)


def test_timeout(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    with pytest.raises(ExtractionFailed, match="timeout"):
        ClaudeCodeExtractor(timeout=0.5).extract(image)


def test_api_key_refused_unless_allowed(fake: Path, image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "structured")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(ExtractorUnavailable, match="ANTHROPIC_API_KEY"):
        ClaudeCodeExtractor().extract(image)
    assert not (fake / "calls").exists()
    assert ClaudeCodeExtractor(allow_api_billing=True).extract(image).extraction.catno == "DKMNTL 031"


def test_missing_executable(image: Path) -> None:
    with pytest.raises(ExtractorUnavailable, match="not found"):
        ClaudeCodeExtractor(executable="/nonexistent/claude").extract(image)
