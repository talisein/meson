# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Meson development team

"""Optional fabrication rung for the compiler-owned-spaced header-unit cell.

The header-unit-on-a-spaced-path tests need a GCC whose *install prefix*
contains a space, so that a system header (``import <vector>;``) resolves to a
spaced path. When the host GCC's prefix is already unspaced and a directory
link to it canonicalizes the space away (GCC resolves the reparse point back to
the real prefix), the only way to get a spaced prefix is a real copy of the
compiler under a spaced path.

This rung makes that copy. It is deliberately kept in its own module and behind
an off-by-default environment gate: this is the optional rung of the
fabrication ladder. Without it -- with the module deleted, or the gate unset --
the ladder in ``CppModulesTestMixin.fabricate_spaced_gcc_prefix`` ends at the
link probe and the affected cells skip loudly rather than break. Nothing else
imports it, and the one import there is is a soft one, so deleting this file is
a supported way to drop the rung.

A full compiler tree is ~1 GB. Meson's own compiler detection links a probe
program, so a compile-only subset (drivers, cc1plus, include chains: ~170 MB)
is not enough to configure with -- the linker, crt objects and runtime
libraries must come too. The linkable subset is ~450 MB and copies in a handful
of seconds, which a CI runner that opts in can afford. Enable with the
environment variable named by ``GATE_ENV`` set to a truthy value; the runner
owns that switch.
"""

from __future__ import annotations
import os
import shutil
import typing as T

# The runner sets this to opt the copy rung in. Unset (the default) leaves the
# ladder ending at the link probe -- see the module docstring.
GATE_ENV = 'MESON_TEST_SPACED_GCC_COPY'

# Tree-relative subpaths a header-unit build AND meson's link-based compiler
# detection need, resolved against the compiler's install root (the parent of
# bin/). Globs so a different GCC minor version's directory name still matches.
#
# These are the *platform-shaped* entries and are hardcoded on purpose: the
# support half of a MinGW tree is spelled the same whatever the drivers are
# called -- cc1plus and its runtime DLLs, the C++/C include chains, the
# binutils linker/assembler, and the crt/runtime libraries the link step reads.
# The *driver* entries are not: their stems carry the install's own
# triple/version decoration, so _driver_patterns derives them from the
# resolved compiler instead of guessing.
_NEEDED_SUPPORT = [
    'bin/ld*.exe', 'bin/as.exe', 'bin/ar.exe', 'bin/*.dll',
    'libexec/gcc/*/*/cc1plus.exe', 'libexec/gcc/*/*/liblto_plugin.dll',
    'include/c++',
    'lib',
    'x86_64-w64-mingw32/include', 'x86_64-w64-mingw32/lib',
]


def _compiler_root(gxx_exe: str) -> T.Optional[str]:
    # bin/g++.exe -> the install root (parent of bin/). None if it is not the
    # bin/ layout this rung knows how to subset.
    real = os.path.realpath(gxx_exe)
    bindir = os.path.dirname(real)
    if os.path.basename(bindir).lower() != 'bin':
        return None
    return os.path.dirname(bindir)


def _driver_rel(gxx_exe: str, root: str) -> str:
    """The real driver's tree-relative spelling, in glob ('/') form."""
    rel = os.path.relpath(os.path.realpath(gxx_exe), root)
    return rel.replace(os.sep, '/')


def _driver_patterns(gxx_exe: str, root: str) -> T.List[str]:
    """The driver entries to copy, derived from the resolved compiler rather
    than assumed. An install's drivers may carry a target triple and/or a
    version around the stem (``x86_64-w64-mingw32-g++.exe``, ``g++-13``);
    whatever decorates the C++ driver decorates its gcc/cpp siblings too, so
    the siblings are spelled with the same prefix and suffix. A driver whose
    name does not contain a ``g++`` stem (a wrapper, say) yields only itself
    -- copying a wrongly-guessed sibling is worse than copying none.
    """
    rel = _driver_rel(gxx_exe, root)
    pats = [rel]
    head, _, name = rel.rpartition('/')
    stem_at = name.find('g++')
    if stem_at >= 0:
        prefix, suffix = name[:stem_at], name[stem_at + len('g++'):]
        for sibling in ('gcc', 'cpp'):
            spelled = f'{prefix}{sibling}{suffix}'
            pats.append(f'{head}/{spelled}' if head else spelled)
    return pats


def available() -> bool:
    """Whether the runner opted this rung in."""
    return bool(os.environ.get(GATE_ENV))


def minimal_gcc_copy(gxx_exe: str, dest_root: str) -> T.Optional[str]:
    """Copy the header-unit-compiling subset of a GCC tree at `gxx_exe` into
    `dest_root` (a directory the caller owns and cleans up, whose path may hold
    a space). Return the copied driver's path, or None when the source is not a
    layout this rung can subset. The caller re-probes the copy to confirm the
    prefix is actually spaced before using it.
    """
    if not available():
        return None
    root = _compiler_root(gxx_exe)
    if root is None:
        return None
    import glob
    os.makedirs(dest_root, exist_ok=True)
    for pat in _driver_patterns(gxx_exe, root) + _NEEDED_SUPPORT:
        for src in glob.glob(os.path.join(root, *pat.split('/'))):
            rel = os.path.relpath(src, root)
            dst = os.path.join(dest_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    # Spell the copied driver the way it is spelled in the source tree; the
    # copy is relative-path-faithful, so the source's own relative spelling is
    # the answer -- re-deriving it from a hardcoded 'bin/g++.exe' would miss
    # every decorated or non-bin/ install.
    gxx = os.path.join(dest_root, os.path.relpath(os.path.realpath(gxx_exe), root))
    return gxx if os.path.isfile(gxx) else None
