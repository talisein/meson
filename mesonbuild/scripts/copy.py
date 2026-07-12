# SPDX-License-Identifier: Apache-2.0
# Copyright © 2021-2023 Intel Corporation
from __future__ import annotations

"""Helper script to copy files at build time.

This is easier than trying to detect whether to use copy, cp, or something else.
"""

import os
import shutil
import typing as T


def run(args: T.List[str]) -> int:
    # --link asks for a hard link, so a file that has to exist under a second
    # name does not cost a second copy of its contents. It falls back to a copy
    # where links are unavailable (a filesystem without them, or a source and
    # destination on different ones). Either way the destination ends up with the
    # source's mtime, so a build that changed nothing stays a no-op.
    link = bool(args) and args[0] == '--link'
    if link:
        args = args[1:]
    try:
        if link:
            os.makedirs(os.path.dirname(args[1]) or '.', exist_ok=True)
            try:
                os.unlink(args[1])
            except FileNotFoundError:
                pass
            try:
                os.link(args[0], args[1])
                return 0
            except OSError:
                pass
        shutil.copy2(args[0], args[1])
    except Exception:
        return 1
    return 0
