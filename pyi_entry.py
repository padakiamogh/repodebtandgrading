"""PyInstaller entry point: preserves package structure for relative imports."""

from repodebt.cli import main

if __name__ == "__main__":
    raise SystemExit(main())