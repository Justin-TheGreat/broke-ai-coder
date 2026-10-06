"""app.ops helpers used by the setup scripts."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from app.config.loader import load_config
from app.ops import setup_values as setup_values_module
from app.ops.setup_values import main as setup_main
from app.ops.setup_values import setup_values
from app.ops.wslconf import main as wslconf_main
from app.ops.wslconf import merge_wsl_conf

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"

INTEROP_OK = "[interop]\nenabled=false\nappendWindowsPath=false\n"
COMPLIANT = INTEROP_OK + '[automount]\noptions="umask=077"\n'


def lines(text):
    return text.splitlines()


def test_empty_input_gets_all_sections():
    out, changes = merge_wsl_conf("")
    assert changes
    assert out == INTEROP_OK + '\n[automount]\noptions="umask=077"\n'
    assert merge_wsl_conf(out)[1] == []


def test_compliant_has_no_changes():
    assert merge_wsl_conf(COMPLIANT) == (COMPLIANT, [])


def test_preserves_comments_unknown_sections_and_keys():
    src = "# top\n[boot]\nsystemd=true\n; semi\n[user]\ndefault=justin\n"
    out, changes = merge_wsl_conf(src)
    assert changes
    for keep in ("# top", "[boot]", "systemd=true", "; semi", "[user]", "default=justin"):
        assert keep in lines(out)
    assert out.startswith(src)


def test_wrong_value_fixed_in_every_occurrence_case_insensitive():
    src = (
        '[Interop]\nEnabled = True\nappendwindowspath = "TRUE"\n'
        "[x]\nenabled=true\n[INTEROP]\nenabled=yes\n"
    )
    out, changes = merge_wsl_conf(src)
    ls = lines(out)
    assert "Enabled = false" in ls and "appendwindowspath = false" in ls
    assert ls[ls.index("[INTEROP]") + 1] == "enabled=false"
    assert ls[ls.index("[x]") + 1] == "enabled=true"  # unrelated section is untouched
    assert merge_wsl_conf(out)[1] == []
    assert any("enabled" in c for c in changes)


def test_quoted_false_is_compliant_for_interop():
    src = (
        '[interop]\nenabled="false"\nappendWindowsPath = FALSE\n[automount]\noptions="umask=077"\n'
    )
    assert merge_wsl_conf(src)[1] == []


def test_missing_key_inserted_after_last_key_of_first_section():
    src = '[interop]\n# c\nenabled=false\n\n[automount]\noptions="umask=077"\n[interop]\nx=1\n'
    out, _ = merge_wsl_conf(src)
    ls = lines(out)
    i = ls.index("enabled=false")
    assert ls[i + 1] == "appendWindowsPath=false"


def test_missing_section_appended_after_blank_line():
    out, changes = merge_wsl_conf('[automount]\noptions="umask=077"\n')
    assert out.endswith("\n\n[interop]\nenabled=false\nappendWindowsPath=false\n")
    assert len(changes) == 2


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ('"metadata,umask=022"', 'options="metadata,umask=077"'),
        ("metadata,umask=077,fmask=11", 'options="metadata,umask=077,fmask=077"'),
        ('"umask=077,dmask=022,fmask=zz"', 'options="umask=077,dmask=077,fmask=077"'),
        ('"metadata"', 'options="metadata,umask=077"'),
        ('"umask=0077"', None),
        ('"umask=077,fmask=177"', None),
    ],
)
def test_automount_options(options, expected):
    src = INTEROP_OK + f"[automount]\noptions={options}\n"
    out, changes = merge_wsl_conf(src)
    got = next(ln for ln in lines(out) if ln.startswith("options"))
    if expected is None:
        assert changes == [] and out == src
    else:
        assert changes and got == expected
        assert merge_wsl_conf(out)[1] == []


def test_automount_missing_key_and_section():
    out, _ = merge_wsl_conf(INTEROP_OK + "[automount]\nenabled=true\n")
    ls = lines(out)
    assert ls[ls.index("enabled=true") + 1] == 'options="umask=077"'
    out2, _ = merge_wsl_conf(INTEROP_OK)
    assert out2.endswith('\n\n[automount]\noptions="umask=077"\n')


def test_crlf_input_gives_lf_output_and_is_idempotent():
    src = "[boot]\r\nsystemd=true\r\n[interop]\r\nenabled=true\r\n"
    out, changes = merge_wsl_conf(src)
    assert changes and "\r" not in out
    out2, changes2 = merge_wsl_conf(out)
    assert changes2 == [] and out2 == out
    assert merge_wsl_conf(COMPLIANT.replace("\n", "\r\n")) == (COMPLIANT, [])


def test_check_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.conf"
    good.write_text(COMPLIANT, encoding="utf-8")
    assert wslconf_main(["check", str(good)]) == 0
    bad = tmp_path / "bad.conf"
    bad.write_text("[interop]\nenabled=true\n", encoding="utf-8")
    assert wslconf_main(["check", str(bad)]) == 1
    assert "enabled" in capsys.readouterr().out
    assert wslconf_main(["check", str(tmp_path / "missing.conf")]) == 1
    assert wslconf_main(["check", str(tmp_path)]) == 2  # a directory is unreadable
    assert wslconf_main(["bogus", str(good)]) == 2


def test_merge_cli_prints_and_never_writes(tmp_path, capsys):
    p = tmp_path / "w.conf"
    p.write_text("[boot]\nsystemd=true\n", encoding="utf-8")
    assert wslconf_main(["merge", str(p)]) == 0
    assert "[interop]" in capsys.readouterr().out
    assert p.read_text(encoding="utf-8") == "[boot]\nsystemd=true\n"


# ---- setup_values -----------------------------------------------------------------------


def test_setup_values_keys_and_order():
    cfg = load_config(EXAMPLE)
    vals = setup_values(cfg)
    assert [k for k, _ in vals] == [
        "OS_USER_MODE",
        "OC_USER",
        "OPENCODE_BIN",
        "SERVER_DIRECTORY",
        "WORKING_DIRECTORY",
        "DATABASE_PATH",
        "MANAGED_SERVER",
    ]
    d = dict(vals)
    assert d["OS_USER_MODE"] == "sudo" and d["OC_USER"] == "brokeoc"
    assert d["OPENCODE_BIN"] == "/usr/local/bin/opencode" and d["MANAGED_SERVER"] == "true"


def test_setup_values_cli_output(capsys, monkeypatch):
    # Windows abspath() would add a drive letter; the scripts only run on Linux.
    fake_os = types.SimpleNamespace(path=types.SimpleNamespace(abspath=lambda p: "/abs/" + p))
    monkeypatch.setattr(setup_values_module, "os", fake_os)
    assert setup_main(["--config", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "OS_USER_MODE=sudo" and len(out) == 7


def test_setup_values_unsafe_value_exits_2(tmp_path, capsys):
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "working_directory: /workspace", 'working_directory: "/work space"'
    )
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text, encoding="utf-8")
    assert setup_main(["--config", str(cfg)]) == 2
    captured = capsys.readouterr()
    assert "unsafe value for WORKING_DIRECTORY" in captured.err and captured.out == ""


def test_setup_values_config_error_exits_2(tmp_path, capsys):
    assert setup_main(["--config", str(tmp_path / "nope.yaml")]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("opencode: {unknown_key: 1}\n", encoding="utf-8")
    assert setup_main(["--config", str(bad)]) == 2
    assert capsys.readouterr().out == ""
