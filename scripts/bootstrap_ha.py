"""Compat shim: bootstrap_ha.py now lives in the package.

The real implementation moved to ``src/sandcastle_sim/bootstrap.py``
so pip-installed users get the same behavior as repo checkouts.
This file is preserved as a thin wrapper so any existing tooling
(Makefiles, cron jobs, snippets in older docs) keeps working.

Run either of these — they're identical:

    python scripts/bootstrap_ha.py
    python -m sandcastle_sim.bootstrap
"""

from __future__ import annotations

import sys

from sandcastle_sim.bootstrap import main


if __name__ == "__main__":
    sys.exit(main())
