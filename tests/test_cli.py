import re


def test_package_import_exposes_version():
    import subctl

    assert isinstance(subctl.__version__, str)
    assert subctl.__version__


def test_subctl_help_works(run_subctl):
    result = run_subctl("--help")

    assert result.returncode == 0, result.stderr
    assert re.search(r"\bsubctl\b", result.stdout, flags=re.IGNORECASE)
    assert "--help" in result.stdout
