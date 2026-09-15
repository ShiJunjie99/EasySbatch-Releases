from scripts.emit_ci_build_failure import diagnostics


def test_diagnostics_selects_error_context_and_redacts_sensitive_lines():
    raw = "\n".join([
        "unrelated start",
        "nearby before",
        "\x1b[31mTS2345: compile error\x1b[0m",
        "API_KEY=must-not-appear",
        "nearby after",
        "unrelated end",
    ])

    value = diagnostics(raw, context=1)

    assert "TS2345: compile error" in value
    assert "nearby before" in value
    assert "must-not-appear" not in value
    assert "[redacted sensitive diagnostic line]" in value
    assert "unrelated start" not in value
    assert "unrelated end" not in value


def test_diagnostics_ignores_error_filenames_but_keeps_real_error_lines():
    raw = "\n".join([
        "lib/types/error.js",
        "lib/types/remote-error.d.ts",
        "ERROR Error: packaging failed",
        "    at build (package-target.ts:10:2)",
    ])

    value = diagnostics(raw, context=0)

    assert value == "ERROR Error: packaging failed"


def test_diagnostics_falls_back_to_log_tail():
    raw = "\n".join(f"line {index}" for index in range(50))

    value = diagnostics(raw)

    assert "line 9" not in value
    assert "line 10" in value
    assert "line 49" in value
