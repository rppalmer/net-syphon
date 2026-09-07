import subprocess
import sys


def test_import_has_no_output_or_home_side_effect(tmp_path):
    """Catch package imports that write files or corrupt stdio MCP framing."""
    result = subprocess.run(
        [sys.executable, "-c", "import net_syphon"],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": ""},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
