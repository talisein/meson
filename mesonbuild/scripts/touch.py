# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 The Meson development team
from __future__ import annotations

"""Helper script to touch stamp files at build time.

This is easier than trying to detect whether to use touch (absent on cmd.exe)
or something else.
"""

import typing as T

from pathlib import Path


def run(args: T.List[str]) -> int:
    try:
        for path in args:
            Path(path).touch()
    except Exception:
        return 1
    return 0
