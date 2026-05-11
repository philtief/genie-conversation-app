"""Minimal smoke test: app.py parses as valid Python."""
import ast
import pathlib


def test_app_parses():
    src = pathlib.Path(__file__).parent / "app.py"
    ast.parse(src.read_text())

if __name__ == "__main__":
    test_app_parses()
    print("OK")
