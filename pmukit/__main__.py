"""`python -m pmukit` -- the entry point the installed launcher execs."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
