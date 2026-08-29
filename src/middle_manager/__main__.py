"""Enable `python -m middle_manager` as the `mm` entry point."""

from middle_manager.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
