# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Meson development team

"""Capability-token framework for the C++ module unittests.

Every C++ module test is gated on the *capability tokens* its fixture needs,
probed once per (compiler id, version) from the detected host C++ compiler.
CI covers the compiler axis by running the suite once per toolchain (CXX set
per job); within one process the tokens subtract the tests the toolchain
cannot run, so no test hand-rolls compiler-version logic.

Token vocabulary (the API for the C++ module test series):

``modules``
    Named modules via the P1689 pipeline are usable: the compiler is one of
    gcc/clang/msvc and ``supports_cpp_modules_p1689()`` holds (GCC >= 14,
    cl >= 19.32, Clang with a feature-probed P1689-capable clang-scan-deps).
    The Ninja-backend requirement is enforced by the gate itself, since the
    backend is per-test-process state, not a compiler capability.
``partitions``, ``header_units``, ``module_interfaces``
    Module partitions, the ``cpp_header_units`` kwarg, and the
    ``cpp_module_interfaces`` kwarg. All currently coincide with ``modules``
    on every supported compiler; they are separate tokens so that a future
    toolchain that splits them only needs a probe change, not test edits.
``import_std``
    ``dependency('std')`` resolves: ``get_std_module_sources()`` finds a
    stdlib module manifest (GCC >= 15 libstdc++.modules.json, Clang with a
    libc++/libstdc++ manifest, an MSVC toolset modules.json) -- except GCC
    15.0-15.2, whose std module is too unreliable for the test suite.
``regex_scanner``
    The legacy regex fallback scanner (mesonbuild/scripts/depscan.py) can
    build a flat named module: the compiler compiles a probe interface unit
    and its importer with one of its bare modules flags
    (``get_cpp_modules_args()``). Probed by actually compiling, not by
    version: in practice GCC qualifies, Clang does not (an interface unit
    needs ``-x c++-module`` there, which the regex path never passes).
``bmionly``
    The compiler can emit a BMI with no object file (GCC ``-fmodule-only``,
    Clang ``--precompile``, cl ``/ifcOnly``). Probed by compiling a trivial
    interface unit and checking a BMI appears and no object does. The
    prerequisite for BMI-only variants.
``bmi_classes``
    Divergent BMI equivalence classes work rather than warn: Meson gives
    each BMI class its own subdirectory of a provider's BMI directory and
    synthesizes BMI-only variants of shared module providers
    (``Compiler.supports_bmi_classes()``, plus the ``bmionly`` probe). Clang,
    MSVC from cl 19.32, and GCC (from 14, the P1689 floor; its BMIs are named
    by Meson's module mapper there).
``ixx_as_cpp``
    The driver compiles a ``.ixx`` source as C++ from its extension alone, with
    no language flag -- what a target that opts a ``.ixx`` out of module
    treatment (``cpp_modules: false``) relies on, since Meson then passes no
    ``-x``. Not a module capability but a driver one, so it is probed
    independently of ``modules``: GCC >= 15 and cl qualify; GCC <= 14 and
    Clang treat the file as linker input.
"""

from __future__ import annotations
import functools
import glob
import json
import os
import re
import subprocess
import tempfile
import textwrap
import typing as T
import unittest

from mesonbuild.compilers import detect_cpp_compiler
from mesonbuild.mesonlib import MachineChoice, is_windows, version_compare, EnvironmentException
from run_tests import get_fake_env, Backend

if T.TYPE_CHECKING:
    from typing_extensions import ParamSpec

    from mesonbuild.compilers.cpp import CPPCompiler
    from .baseplatformtests import BasePlatformTests

    P = ParamSpec('P')
    R = T.TypeVar('R')

CPP_MODULE_CAPS = frozenset({
    'modules', 'partitions', 'header_units', 'module_interfaces',
    'import_std', 'regex_scanner', 'bmionly', 'bmi_classes', 'ixx_as_cpp',
})

_caps_cache: T.Dict[T.Tuple[str, str], T.FrozenSet[str]] = {}
_regex_flag_cache: T.Dict[T.Tuple[str, str], T.Optional[str]] = {}
_bmionly_cache: T.Dict[T.Tuple[str, str], bool] = {}
_ixx_as_cpp_cache: T.Dict[T.Tuple[str, str], bool] = {}


@functools.lru_cache(maxsize=None)
def _host_cpp_compiler() -> T.Optional[CPPCompiler]:
    try:
        return T.cast('CPPCompiler', detect_cpp_compiler(get_fake_env(), MachineChoice.HOST))
    except EnvironmentException:
        return None


def _regex_probe_builds(cpp: CPPCompiler, flag: str) -> bool:
    """Compile a two-TU named-module hello with only the given bare flag,
    the way the regex fallback path would (plain compiles, default module
    mapper/cache in cwd)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'mod.cpp'), 'w', encoding='utf-8') as f:
            f.write('export module regexprobe;\nexport int probefunc() { return 42; }\n')
        with open(os.path.join(tmpdir, 'use.cpp'), 'w', encoding='utf-8') as f:
            f.write('import regexprobe;\nint main() { return probefunc() - 42; }\n')
        exelist = cpp.get_exelist(ccache=False)
        if cpp.get_argument_syntax() == 'msvc':
            std_args = ['/std:c++20', '/c']
            consumer_flags = []
        else:
            std_args = ['-std=c++20', '-c']
            consumer_flags = [flag]
        for src, flags in (('mod.cpp', [flag]), ('use.cpp', consumer_flags)):
            try:
                p = subprocess.run(exelist + std_args + flags + [src],
                                   cwd=tmpdir, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if p.returncode != 0:
                return False
    return True


def regex_scanner_flag(cpp: CPPCompiler) -> T.Optional[str]:
    """The bare modules flag with which this compiler can build a flat named
    module on the regex fallback path, or None if none works."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _regex_flag_cache[key]
    except KeyError:
        pass
    flag = next((f for f in cpp.get_cpp_modules_args() if _regex_probe_builds(cpp, f)), None)
    _regex_flag_cache[key] = flag
    return flag


def _bmionly_probe_builds(cpp: CPPCompiler) -> bool:
    """Compile a trivial interface unit in the compiler's BMI-only mode and
    check that a BMI appears and no object does (a failing rc, a missing BMI
    or a stray object all mean the mode is unusable)."""
    family = cpp.cpp_module_family()
    if family == 'msvc':
        src = 'mod.ixx'
        attempts = [['/nologo', '/std:c++20', '/c', '/interface', '/ifcOnly', src]]
    elif family == 'clang':
        src = 'mod.cppm'
        attempts = [['-std=c++20', '--precompile', src, '-o', 'mod.pcm']]
    else:
        src = 'mod.cpp'
        attempts = [[flag, '-std=c++20', '-fmodule-only', '-c', src]
                    for flag in cpp.get_cpp_modules_args()]
    exelist = cpp.get_exelist(ccache=False)
    for args in attempts:
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, src), 'w', encoding='utf-8') as f:
                f.write('export module bmiprobe;\nexport int probefunc() { return 42; }\n')
            try:
                p = subprocess.run(exelist + args, cwd=tmpdir,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if p.returncode != 0:
                continue
            produced = [name for root, _, files in os.walk(tmpdir) for name in files]
            if any(name.endswith(cpp.get_module_bmi_suffix()) for name in produced) \
                    and not any(name.endswith(('.o', '.obj')) for name in produced):
                return True
    return False


def bmionly_works(cpp: CPPCompiler) -> bool:
    """Whether this compiler can emit a BMI without an object file."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _bmionly_cache[key]
    except KeyError:
        pass
    result = _bmionly_probe_builds(cpp)
    _bmionly_cache[key] = result
    return result


def _ixx_as_cpp_probe_builds(cpp: CPPCompiler) -> bool:
    """Compile a trivial non-module .ixx with no language flag and check an
    object appears. The rc alone is not enough: a driver that does not know
    the extension takes the file as linker input, warns, and exits 0 under -c
    without writing anything."""
    if cpp.get_argument_syntax() == 'msvc':
        args = ['/nologo', '/c', 'probe.ixx']
    else:
        args = ['-c', 'probe.ixx', '-o', 'probe.o']
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'probe.ixx'), 'w', encoding='utf-8') as f:
            f.write('int probefunc() { return 42; }\n')
        try:
            p = subprocess.run(cpp.get_exelist(ccache=False) + args, cwd=tmpdir,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return p.returncode == 0 and any(
            name.endswith(('.o', '.obj')) for name in os.listdir(tmpdir))


def ixx_as_cpp_works(cpp: CPPCompiler) -> bool:
    """Whether this compiler's driver compiles a .ixx as C++ by extension."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _ixx_as_cpp_cache[key]
    except KeyError:
        pass
    result = _ixx_as_cpp_probe_builds(cpp)
    _ixx_as_cpp_cache[key] = result
    return result


def cpp_module_caps(cpp: CPPCompiler) -> T.FrozenSet[str]:
    """The capability tokens of the given compiler; see the module docstring
    for the vocabulary. Memoized per (id, version); import_std, regex_scanner,
    bmionly and ixx_as_cpp probe by invoking the compiler."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _caps_cache[key]
    except KeyError:
        pass
    caps = set()
    # Deliberately not gated on current_vs_supports_modules() or the
    # >= 19.28.28617 floor: no test depends on them and a broken developer
    # prompt should fail loudly, not skip.
    if cpp.cpp_module_family() != 'none' and cpp.supports_cpp_modules_p1689():
        caps.update(('modules', 'partitions', 'header_units', 'module_interfaces'))
        if cpp.get_std_module_sources():
            # GCC 15.0-15.2 ship the manifest but are too unreliable for the
            # std module.
            if cpp.cpp_module_family() != 'gcc' or version_compare(cpp.version, '>=15.3'):
                caps.add('import_std')
    if regex_scanner_flag(cpp) is not None:
        caps.add('regex_scanner')
    if 'modules' in caps and bmionly_works(cpp):
        caps.add('bmionly')
        if cpp.supports_bmi_classes():
            caps.add('bmi_classes')
    if ixx_as_cpp_works(cpp):
        caps.add('ixx_as_cpp')
    result = frozenset(caps)
    _caps_cache[key] = result
    return result


def require_cpp_module_caps(test: BasePlatformTests, *tokens: str,
                            compiler: T.Union[str, T.Tuple[str, ...], None] = None) -> CPPCompiler:
    """Skip the running test unless the Ninja backend is in use, the host C++
    compiler is one of `compiler` (id string or tuple of ids, if given), and
    it has every requested capability token. Returns the compiler."""
    unknown = set(tokens) - CPP_MODULE_CAPS
    if unknown:
        raise ValueError(f'Unknown C++ module capability token(s): {sorted(unknown)}')
    if test.backend is not Backend.ninja:
        raise unittest.SkipTest(f'C++ modules only work with the Ninja backend (not {test.backend.name}).')
    cpp = _host_cpp_compiler()
    if cpp is None:
        raise unittest.SkipTest('No C++ compiler found.')
    if compiler is not None:
        ids = (compiler,) if isinstance(compiler, str) else tuple(compiler)
        if cpp.get_id() not in ids:
            raise unittest.SkipTest(f'Test only applies to {"/".join(ids)} (found {cpp.get_id()}).')
    caps = cpp_module_caps(cpp)
    missing = [t for t in tokens if t not in caps]
    if missing:
        raise unittest.SkipTest(f'C++ module capability {", ".join(repr(m) for m in missing)} '
                                f'not available with {cpp.get_id()} {cpp.version}.')
    return cpp


def requires_cpp_module_caps(*tokens: str,
                             compiler: T.Union[str, T.Tuple[str, ...], None] = None
                             ) -> T.Callable[[T.Callable[P, R]], T.Callable[P, R]]:
    """Decorator form of require_cpp_module_caps."""
    def wrapper(func: T.Callable[P, R]) -> T.Callable[P, R]:
        @functools.wraps(func)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            require_cpp_module_caps(T.cast('BasePlatformTests', args[0]), *tokens, compiler=compiler)
            return func(*args, **kwargs)
        return wrapped
    return wrapper


# The flag that names a specific BMI on a consumer command line. Commands must
# never carry one: modules are mapped via the module mapper / prebuilt-path
# machinery, not per-BMI flags, or the command lines would change with the
# module graph. Keyed by cpp_module_family(), not by compiler id: the families
# are exactly the three that have such a flag ('none' never reaches a module
# test), while an id would raise KeyError on every clang-derived compiler that
# carries an id of its own -- intel-llvm, armltdclang, emscripten.
_BMI_MAP_FLAG = {'gcc': '-fmodule-file', 'clang': '-fmodule-file', 'msvc': '/reference'}


class CppModulesTestMixin:

    """Shared driver for C++ module tests; mix into a BasePlatformTests
    subclass. Test bodies find a provider's public BMIs in its own
    meson-private/<id>@bmi-public/<BMI class digest>/ directory, named with
    the detected compiler's BMI suffix, so the same method works under
    whichever single compiler the process was launched with."""

    # Rebuild tests exercise Meson's dependency graph, so ccache must not
    # answer for the compiler: it does not track the contents of BMIs, so a
    # cached hit can hand back an object compiled against the pre-edit
    # module -- including via distro PATH masquerade (/usr/lib64/ccache).
    NO_CCACHE = {'CCACHE_DISABLE': '1'}

    def host_cpp_compiler(self) -> CPPCompiler:
        cpp = _host_cpp_compiler()
        assert cpp is not None, 'gate the test with require(s)_cpp_module_caps first'
        return cpp

    def _bmi_file(self, module_name: str) -> str:
        # Partition ':' maps to '-' as in the documented scheme.
        return module_name.replace(':', '-') + self.host_cpp_compiler().get_module_bmi_suffix()

    def public_bmi_roots(self, provider: T.Optional[str] = None) -> T.List[str]:
        """The `<id>@bmi-public` directories of `provider` (a target name;
        every providing target when None), sorted. A target id is its name
        and type suffix, behind a subdir digest unless it is at the top
        level."""
        priv = glob.escape(os.path.join(self.builddir, 'meson-private'))
        name = '*' if provider is None else glob.escape(provider)
        return sorted({*glob.glob(os.path.join(priv, f'{name}@*@bmi-public')),
                       *glob.glob(os.path.join(priv, f'*@@{name}@*@bmi-public'))})

    def _provider_root(self, provider: str) -> str:
        roots = self.public_bmi_roots(provider)
        self.assertEqual(len(roots), 1, f'expected one public BMI directory of {provider}, got {roots}')
        return roots[0]

    def bmi_path(self, module_name: str, target: T.Optional[str] = None) -> str:
        """Where the BMI of the given module lands: the one BMI-class subdir
        of its provider's public directory holding it. `target` names the
        provider, and must when more than one target provides the name."""
        roots = [self._provider_root(target)] if target is not None else self.public_bmi_roots()
        matches = [m for r in roots for m in glob.glob(os.path.join(glob.escape(r), '*', self._bmi_file(module_name)))]
        self.assertEqual(len(matches), 1,
                         f'expected one public BMI of {module_name}, got {matches}'
                         + ('' if target is not None else ' (pass target= to pick a provider)'))
        return matches[0]

    def public_bmi_matches(self, module_name: str) -> T.List[str]:
        """Every public BMI of the given module anywhere in the build, flat
        or in a BMI-class subdir of any target's public directory; [] means no
        target publishes it."""
        bmi = self._bmi_file(module_name)
        return sorted(m for r in self.public_bmi_roots()
                      for m in glob.glob(os.path.join(glob.escape(r), bmi))
                      + glob.glob(os.path.join(glob.escape(r), '*', bmi)))

    def build_and_check_modules(self, testdir_name: str, *,
                                srcdir: T.Optional[str] = None,
                                extra_args: T.Sequence[str] = (),
                                setup_contains: T.Sequence[str] = (),
                                setup_not_contains: T.Sequence[str] = (),
                                build_contains: T.Sequence[str] = (),
                                build_not_contains: T.Sequence[str] = (),
                                run_tests: bool = True,
                                bmis: T.Sequence[str] = (),
                                ninja_args_not_contains: T.Optional[T.Sequence[str]] = None,
                                ninja_not_contains: T.Sequence[str] = (),
                                noop_check: bool = True,
                                no_ccache: bool = False) -> str:
        """The common spine of a module test: configure, build, run the
        tests, then check the BMI cache and build.ninja.

        srcdir: a pre-copied fixture path to configure from directly (e.g. a
            deterministically spaced tmp copy); the name still labels messages.
            When None the name resolves under the unit-test dir as usual.
        bmis: logical module names whose BMIs must exist, each in the one
            public BMI directory publishing it (bmi_path).
        ninja_args_not_contains: needles no 'ARGS =' line may contain; None
            selects the compiler default (the BMI suffix and the flag that
            names a BMI), () disables the check (header units legitimately
            map BMIs on consumer command lines).
        ninja_not_contains: needles the whole of build.ninja may not contain.
        noop_check: an untouched follow-up build must do no work at all --
            catches any module output rewritten without copy-if-different.

        Returns the build output.
        """
        cpp = self.host_cpp_compiler()
        testdir = srcdir if srcdir is not None else os.path.join(self.unit_test_dir, testdir_name)
        out = self.init(testdir, extra_args=list(extra_args))
        for needle in setup_contains:
            self.assertIn(needle, out)
        for needle in setup_not_contains:
            self.assertNotIn(needle, out)
        built = self.build(override_envvars=self.NO_CCACHE if no_ccache else None)
        for needle in build_contains:
            self.assertIn(needle, built)
        for needle in build_not_contains:
            self.assertNotIn(needle, built)
        if run_tests:
            self.run_tests()
        for name in bmis:
            path = self.bmi_path(name)
            self.assertTrue(os.path.isfile(path), f'missing BMI {path}')
        if ninja_args_not_contains is None:
            ninja_args_not_contains = (cpp.get_module_bmi_suffix(),
                                       _BMI_MAP_FLAG[cpp.cpp_module_family()])
        if ninja_args_not_contains or ninja_not_contains:
            with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
                contents = f.read()
            for line in contents.splitlines():
                if line.strip().startswith('ARGS ='):
                    for needle in ninja_args_not_contains:
                        self.assertNotIn(needle, line)
            for needle in ninja_not_contains:
                self.assertNotIn(needle, contents)
        if noop_check:
            self.assertBuildIsNoop()
        return built

    def check_module_rebuild(self, testdir_name: str, *, edit_file: str,
                             srcdir: T.Optional[str] = None,
                             expect_in_rebuild: T.Sequence[str] = ('Linking target prog',),
                             prog: str = 'prog') -> None:
        """Editing a module interface (or a header behind a header unit) must
        rebuild the BMI and recompile every importer, exactly as editing a
        normally #included header does -- otherwise consumers link against a
        stale BMI. The edit bumps the exported value, so a stale build makes
        prog exit nonzero.

        srcdir: a pre-copied, writable fixture path to edit and build in place
            (a fresh copytree, so editing it never touches the original); the
            name still labels messages. When None the name is resolved under the
            unit-test dir and copied here as usual."""
        if srcdir is None:
            srcdir = self.copy_srcdir(os.path.join(self.unit_test_dir, testdir_name))
        self.init(srcdir)
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        path = os.path.join(srcdir, edit_file)
        with open(path, encoding='utf-8') as f:
            content = f.read()
        newcontent = content.replace('return ', 'return 1000 + ', 1)
        self.assertNotEqual(content, newcontent, 'edit was a no-op')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(newcontent)
        out = self.build(override_envvars=self.NO_CCACHE)
        for needle in expect_in_rebuild:
            self.assertIn(needle, out)
        exe = os.path.join(self.builddir, prog + ('.exe' if is_windows() else ''))
        self.assertNotEqual(0, subprocess.run([exe]).returncode,
                            'importer did not pick up the module change (stale BMI)')

    def check_module_graph_mutation(self, testdir_name: str) -> None:
        """Mutate the module graph across rebuilds: add a second interface
        source, rename its file (module name unchanged, also covering
        file name != module name on the kwarg path), then remove it. Each
        phase is a reconfigure -- source lists changed -- and must yield a
        correct program and a clean no-op follow-up build. This exercises the
        collator's provider-claim handling as providers appear, move and
        disappear. The removed module's stale BMI is deliberately not
        asserted gone: nothing cleans the harvest cache."""
        testdir = os.path.join(self.unit_test_dir, testdir_name)
        srcdir = self.copy_srcdir(testdir)
        build_template = textwrap.dedent('''\
            project('cpp module graph mutation', 'cpp', default_options: ['cpp_std=c++20'])

            modlib = static_library('modlib', {srcs}, cpp_module_interfaces: [{srcs}])
            prog = executable('prog', 'main.cpp', link_with: modlib)
            test('prog', prog)
            ''')
        base_main = 'import mymod;\n\nint main() {\n    return modfunc() - 42;\n}\n'
        extra_main = ('import mymod;\nimport extra;\n\n'
                      'int main() {\n    return modfunc() - 42 + extraval();\n}\n')
        extra_src = 'export module extra;\n\nexport int extraval() {\n    return 0;\n}\n'

        def write(name: str, content: str) -> None:
            with open(os.path.join(srcdir, name), 'w', encoding='utf-8') as f:
                f.write(content)

        def build_and_verify() -> str:
            out = self.build(override_envvars=self.NO_CCACHE)
            self.run_tests()
            self.assertBuildIsNoop()
            return out

        self.init(srcdir)
        build_and_verify()

        write('extra.cc', extra_src)
        write('main.cpp', extra_main)
        write('meson.build', build_template.format(srcs="'mod.cc', 'extra.cc'"))
        out = build_and_verify()
        self.assertIn('Linking target prog', out)

        os.rename(os.path.join(srcdir, 'extra.cc'), os.path.join(srcdir, 'other.cc'))
        write('meson.build', build_template.format(srcs="'mod.cc', 'other.cc'"))
        build_and_verify()

        os.unlink(os.path.join(srcdir, 'other.cc'))
        write('main.cpp', base_main)
        write('meson.build', build_template.format(srcs="'mod.cc'"))
        build_and_verify()

    def link_edge(self, target: str) -> str:
        """The given output's build.ninja edge block (its 'build <target>:'
        line plus indented continuations), or '' when absent. The space form
        covers implicit outputs ('build prog.exe | prog.pdb: ...')."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f'build {target}:') or line.startswith(f'build {target} '):
                block = [line]
                for nxt in lines[i + 1:]:
                    if not nxt.startswith((' ', '\t')):
                        break
                    block.append(nxt)
                return '\n'.join(block)
        return ''

    def assert_std_link_edges(self, linked: T.Sequence[str], not_linked: T.Sequence[str]) -> None:
        """dependency('std') synthesizes one static library carrying the std
        module objects; it must be on the link line of every target that
        consumes it, directly or transitively, and of nothing else."""
        stdlib = 'lib__meson_cxx_std.a'
        self.assertTrue(os.path.isfile(os.path.join(self.builddir, stdlib)),
                        'std module static library not built')
        for target in linked:
            self.assertIn(stdlib, self.link_edge(target), f'{target} does not link the std library')
        for target in not_linked:
            self.assertNotIn(stdlib, self.link_edge(target))

    def count_on_link_line(self, target: str, needle: str) -> int:
        """Occurrences of needle on the given output's LINK_ARGS line (the
        edge block also names archives as ninja inputs, which is not the
        link command)."""
        return sum(line.count(needle) for line in self.link_edge(target).splitlines()
                   if line.strip().startswith('LINK_ARGS ='))

    def bmi_variant_ids(self) -> T.Set[str]:
        """The distinct BMI-only variant private dirs named in build.ninja
        (one per provider x divergent class). Tolerates either path separator
        and normalizes to '/' so counts are separator-independent."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        return {m.replace('\\', '/')
                for m in re.findall(r'meson-private[/\\][^/\\\s]*@bmi@[0-9a-f]{12}', contents)}

    def provided_modules(self, target: str) -> T.Set[str]:
        """The module names a target publishes to targets linking it, from its
        provided-modules.json. A private module is never in here (it has no
        importer outside the target), whether it is private by declaration or
        because nothing can link the target at all."""
        # The private dir carries the target's output name (app.p / app.exe.p
        # for an executable, libmylib.a.p / mylib.lib.p for a static library) --
        # glob rather than hardcode any of them.
        patterns = [os.path.join(self.builddir, f'{target}.p', 'provided-modules.json'),
                    os.path.join(self.builddir, f'{target}.*.p', 'provided-modules.json'),
                    os.path.join(self.builddir, f'lib{target}.*.p', 'provided-modules.json')]
        matches = [m for pat in patterns for m in glob.glob(pat)]
        self.assertEqual(len(matches), 1,
                         f'{" or ".join(patterns)}: expected one provmap, got {matches}')
        with open(matches[0], encoding='utf-8') as f:
            return set(json.load(f))

    def private_bmi_dirs(self) -> T.Set[str]:
        """The distinct module-providing-executable private BMI dirs named in
        build.ninja (one per such executable). Tolerates either path
        separator and normalizes to '/' so counts are separator-independent."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        return {m.replace('\\', '/')
                for m in re.findall(r'meson-private[/\\][^/\\\s]*@bmi-private', contents)}

    def header_unit_digests(self, basename: str, *, edges: str = '') -> T.Set[str]:
        """The distinct digests of 'meson-private/header-units/<basename>.<digest>.*'
        paths in build.ninja, one per (mode, spelling) and, on per-class
        compilers, per BMI class. `edges` restricts the search to edge blocks
        whose 'build' line contains it (e.g. 'prog.p/' for one target's
        compiles and scans, '@bmi@' for BMI-variant edges)."""
        pattern = re.compile(r'header-units[/\\]' + re.escape(basename) + r'\.([0-9a-f]{16})\.')
        digests: T.Set[str] = set()
        wanted = False
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build '):
                    wanted = edges in line
                if wanted:
                    digests.update(pattern.findall(line))
        return digests

    def header_unit_bmis(self, basename: str) -> T.Set[str]:
        """Every header-unit BMI the build declares for `basename`, from the
        outputs of the header-unit rule, on every compiler."""
        outputs: T.Set[str] = set()
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if not line.startswith('build ') or 'HEADER_UNIT' not in line:
                    continue
                out = line[len('build '):].partition(': ')[0].strip()
                if basename in os.path.basename(out):
                    outputs.add(out.replace('\\', '/'))
        return outputs

    def header_unit_edge_orderdeps(self, basename: str) -> T.Tuple[T.Optional[str], T.List[str]]:
        """The (output, order-only deps) of the header-unit edge for `basename`.
        A ninja build line's order-only inputs follow the '||'; the header
        basename here contains no space, so a plain split is enough."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if not line.startswith('build ') or 'HEADER_UNIT' not in line:
                    continue
                out, _, rest = line[len('build '):].partition(': ')
                if basename not in os.path.basename(out.strip()):
                    continue
                _, _, orderonly = rest.partition('||')
                return out.strip(), orderonly.split()
        return None, []

    def check_generated_header_unit_ordered(self) -> None:
        """A header unit whose header a custom_target writes during the build.
        GCC cannot name such a unit (it is named by the path it resolves to,
        which does not exist yet) and rejects it at setup; cl and clang have no
        such gate and build it. The unit edge reads the header off disk but
        names no source, so it must order behind the generator -- the bug this
        covers is an edge with no input at all, which ninja is free to run
        before the generator, failing to open the header."""
        testdir = os.path.join(self.unit_test_dir, '198 unresolvable header unit')
        self.init(testdir, extra_args=['-Dmode=generated'])
        out, orderdeps = self.header_unit_edge_orderdeps('generated.h')
        self.assertIsNotNone(out, 'no header-unit edge emitted for generated.h')
        self.assertIn('generated.h', orderdeps,
                      'the header-unit edge does not order behind its generator')
        # Building just the unit from a clean tree must run the generator
        # first: the deterministic form of the race a full -j build only
        # sometimes lost (the generator usually beat the compiler's startup).
        self.build(target=out)
        self.assertTrue(os.path.isfile(os.path.join(self.builddir, 'generated.h')),
                        'building the unit did not run its generator first')

    def header_unit_bmis_of(self, target: str, basename: str) -> T.Set[str]:
        """The header-unit BMIs `target`'s own compiles resolve for `basename`.
        Read from its per-TU mapper tables on GCC, where no unit BMI path
        reaches a command line; from the compile edge's flags elsewhere."""
        if self.host_cpp_compiler().cpp_module_family() != 'gcc':
            return self.header_unit_digests(basename, edges=f'{target}.p/')
        bmis: T.Set[str] = set()
        # The private dir carries the target's output suffix (prog.p on
        # POSIX, prog.exe.p on Windows) -- glob rather than hardcode it. The
        # dot before the wildcard keeps 'prog' from also matching 'progfoo's
        # directory.
        patterns = [os.path.join(self.builddir, f'{target}.p'),
                    os.path.join(self.builddir, f'{target}.*.p')]
        matches = [m for pat in patterns for m in glob.glob(pat)]
        self.assertEqual(len(matches), 1,
                         f'{" or ".join(patterns)}: expected one private dir, got {matches}')
        tdir = matches[0]
        rel = os.path.relpath(tdir, self.builddir).replace('\\', '/') + '/'
        for out, table in self.gcc_program_tables().items():
            if not out.startswith(rel):
                continue
            with open(os.path.join(self.builddir, table), encoding='utf-8') as f:
                units = json.load(f)['header_units']
            bmis.update(b for real, b in units.items()
                        if os.path.basename(real) == basename)
        return bmis

    def bmi_root(self, provider: str) -> str:
        """The directory whose BMI-class subdirectories hold `provider`'s
        public BMIs: its own public directory."""
        return self._provider_root(provider)

    def bmi_class_dirs(self, provider: str) -> T.List[str]:
        """The BMI-class subdirectories `provider`'s public BMIs live in,
        sorted: always at least one, even in a single-BMI-class build."""
        root = self.bmi_root(provider)
        return sorted(e for e in os.listdir(root)
                      if os.path.isdir(os.path.join(root, e)))

    def assert_no_flat_bmi(self, provider: str, module_name: str) -> None:
        """A public BMI must never sit directly in the directory its
        BMI-class subdirectories live in: public BMI directories are always
        keyed by BMI class."""
        flat = os.path.join(self.bmi_root(provider), self._bmi_file(module_name))
        self.assertFalse(os.path.exists(flat), f'flat BMI {flat} must not exist')

    def assert_variants_emit_no_objects(self) -> None:
        """A BMI-only variant must never produce an object file: consumers
        link the original provider's objects, and a second copy of a module's
        initializer or vague-linkage entities in one link is an ODR
        violation."""
        for root, _, files in os.walk(os.path.join(self.builddir, 'meson-private')):
            if '@bmi@' not in root:
                continue
            objs = [f for f in files if f.endswith(('.o', '.obj'))]
            self.assertEqual(objs, [], f'BMI variant dir {root} must not contain objects')

    def check_bmi_classes(self, testdir_name: str, *, module_name: str,
                          provider: str, provider_lib: str, consumers: T.Sequence[str],
                          expected_targets: T.Sequence[str],
                          extra_args: T.Sequence[str] = ()) -> None:
        """The canonical two-class fixture: one module provider, one consumer
        in the provider's own BMI class, one divergent consumer. Divergence
        must build and run, with exactly one
        object-producing provider (the divergent consumer resolves against a
        BMI-only variant), one BMI per class dir, no flat BMI, and exactly
        one variant build-wide -- the compatible consumer must reuse the
        provider's BMI, not spawn a redundant variant."""
        self.build_and_check_modules(testdir_name, extra_args=extra_args)
        bmi = self._bmi_file(module_name)
        root = self.bmi_root(provider)
        class_dirs = self.bmi_class_dirs(provider)
        self.assertEqual(len(class_dirs), 2, f'expected two BMI class dirs, got {class_dirs}')
        for d in class_dirs:
            self.assertTrue(os.path.isfile(os.path.join(root, d, bmi)),
                            f'missing {bmi} in class dir {d}')
        self.assert_no_flat_bmi(provider, module_name)
        targets = sorted(t['name'] for t in self.introspect('--targets'))
        self.assertEqual(targets, sorted(expected_targets),
                         'variants must not appear as targets')
        variants = self.bmi_variant_ids()
        self.assertEqual(len(variants), 1,
                         f'expected exactly one BMI variant, got {sorted(variants)}')
        for t in consumers:
            self.assertEqual(self.count_on_link_line(t, provider_lib), 1,
                             f'{t} must link the real provider exactly once')
            self.assertNotIn('@bmi@', self.link_edge(t), f'{t} must not link variant artifacts')
        self.assert_variants_emit_no_objects()

    def check_import_std_bmi_classes(self, testdir_name: str, *, progs: T.Sequence[str],
                                     compat_progs: T.Sequence[str] = (),
                                     compat_in_all_classes: bool = False,
                                     extra_args: T.Sequence[str] = ()) -> None:
        """Two dialects sharing dependency('std') in one build: one
        __meson_cxx_std target provides the objects for both, each class dir
        carries its own std BMI set (std.compat only where imported, except
        compat_in_all_classes below), and exactly one variant exists -- all
        through the generic variant rule, with no std-specific machinery.

        compat_in_all_classes: on MSVC the provider's compile writes its
        class's BMI eagerly (directory /ifcOutput), so std.compat appears in
        the provider's class dir whether or not anything there imports it;
        Clang's harvest is lazily pulled by importers only.
        """
        cpp = self.host_cpp_compiler()
        suffix = cpp.get_module_bmi_suffix()
        self.build_and_check_modules(testdir_name, extra_args=extra_args)
        self.assert_std_link_edges(progs, ())
        for t in progs:
            self.assertEqual(self.count_on_link_line(t, 'lib__meson_cxx_std.a'), 1,
                             f'{t} must link the std library exactly once')
        std_targets = [t for t in self.introspect('--targets') if t['name'] == '__meson_cxx_std']
        self.assertEqual(len(std_targets), 1, 'exactly one std module target must exist')
        cache = self.bmi_root('__meson_cxx_std')
        class_dirs = self.bmi_class_dirs('__meson_cxx_std')
        self.assertEqual(len(class_dirs), 2, f'expected two BMI class dirs, got {class_dirs}')
        for d in class_dirs:
            self.assertTrue(os.path.isfile(os.path.join(cache, d, 'std' + suffix)),
                            f'missing std{suffix} in class dir {d}')
        if compat_progs:
            compat = [d for d in class_dirs
                      if os.path.isfile(os.path.join(cache, d, 'std.compat' + suffix))]
            if compat_in_all_classes:
                self.assertEqual(compat, class_dirs,
                                 'std.compat must be published in every class dir')
            else:
                self.assertEqual(len(compat), 1,
                                 'std.compat must be published only in the class that imports it')
        self.assertEqual(len(self.bmi_variant_ids()), 1,
                         'expected exactly one BMI variant of the std target')
        self.assert_variants_emit_no_objects()

    def target_output(self, target: str) -> str:
        """`target`'s output file (build-dir relative, '/'-separated), as
        introspection names it: 'prog' or 'prog.exe', 'libfoo.a', ..."""
        files = [t['filename'][0] for t in self.introspect('--targets') if t['name'] == target]
        self.assertEqual(len(files), 1, f'expected one target named {target}, got {files}')
        return os.path.relpath(files[0], self.builddir).replace('\\', '/')

    def resolved_bmi(self, consumer: str, module_name: str) -> str:
        """The BMI (build-dir relative, '/'-separated) that `consumer`'s
        compiles resolve `module_name` to. Every compile of the target that
        imports it must agree."""
        family = self.host_cpp_compiler().cpp_module_family()
        # A target's private directory is its output file's name plus '.p'.
        pdir = self.target_output(consumer) + '.p/'
        found: T.Set[str] = set()
        if family == 'gcc':
            # Named by exact path in each TU's mapper table.
            for out, table in self.gcc_program_tables().items():
                if not out.replace('\\', '/').startswith(pdir):
                    continue
                with open(os.path.join(self.builddir, table), encoding='utf-8') as f:
                    bmi = json.load(f)['modules'].get(module_name)
                if bmi is not None:
                    found.add(bmi.replace('\\', '/'))
        else:
            # Found by searching the compile's directories in turn: exactly
            # one of them may hold it, or the search order would decide.
            # build.ninja may quote each argument ("/ifcSearchDir" "dir" on
            # Windows), so a directory ends at a quote as well as a space.
            search_flag = {'clang': r'-fprebuilt-module-path=([^"\s]+)',
                           'msvc': r'/ifcSearchDir"? "?([^"\s]+)'}[family]
            bmi_file = self._bmi_file(module_name)
            for out, args in self.module_compile_args().items():
                if not out.replace('\\', '/').startswith(pdir):
                    continue
                hits = [f'{d}/{bmi_file}' for d in re.findall(search_flag, args)
                        if os.path.isfile(os.path.join(self.builddir, d, bmi_file))]
                self.assertLessEqual(len(hits), 1, f'{out}: {module_name} is in more than one search dir: {hits}')
                found.update(h.replace('\\', '/') for h in hits)
        self.assertEqual(len(found), 1, f'{consumer} must resolve {module_name} to one BMI, got {found}')
        return found.pop()

    def assert_resolves_from(self, consumer: str, module_name: str, provider: str) -> None:
        """`consumer` resolves `module_name` from `provider`'s own public BMIs."""
        root = os.path.relpath(self.bmi_root(provider), self.builddir).replace('\\', '/')
        bmi = self.resolved_bmi(consumer, module_name)
        self.assertTrue(bmi.startswith(root + '/'), f'{consumer} resolves {module_name} to {bmi}, not under {root}')

    def check_duplicate_module_names(self) -> None:
        """Two unrelated targets in one BMI class each provide the module
        "duplicate": both build, each consumer runs against the provider it
        links, and each provider publishes its own BMI."""
        self.build_and_check_modules('219 duplicate module names')
        for provider, consumer in (('dup1', 'use1'), ('dup2', 'use2')):
            self.assertTrue(os.path.isfile(self.bmi_path('duplicate', target=provider)))
            self.assert_resolves_from(consumer, 'duplicate', provider)
        self.assertEqual(len(self.public_bmi_matches('duplicate')), 2)

    def check_duplicate_module_names_one_link(self) -> None:
        """A single link reaching both providers of one name is still the
        collate's within-link error."""
        self.init(os.path.join(self.unit_test_dir, '219 duplicate module names'),
                  extra_args=['-Dmode=linkboth'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('provided by more than one target reaching this link', cm.exception.stdout)

    def check_both_libraries_module_compiled_twice(self) -> None:
        """both_libraries() whose halves cannot share objects compiles its
        interface twice in one BMI class: two providers of one name, each
        consumer linking one."""
        self.build_and_check_modules('219 duplicate module names',
                                     extra_args=['-Dmode=bothlibs', '-Db_staticpic=false'])
        self.assertEqual(len(self.public_bmi_roots('dup')), 2)
        self.assertEqual(len(self.public_bmi_matches('duplicate')), 2)

    def check_module_moves_between_providers(self) -> None:
        """A module that moves from one linked provider to another leaves no
        BMI behind in the first: its collate prunes it, so a consumer
        linking both finds only the new one, whatever the search order."""
        self.init(os.path.join(self.unit_test_dir, '221 module moves between providers'))
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        self.assertTrue(os.path.isfile(self.bmi_path('shadowed', target='aprov')))
        self.setconf('-Dmode=moved')
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        self.assertBuildIsNoop()
        self.assertEqual(self.public_bmi_matches('shadowed'),
                         [self.bmi_path('shadowed', target='bprov')])

    def check_module_edges_use_response_files(self, extra_args: T.Sequence[str] = ()) -> None:
        """Every edge carrying a target's module search directories -- its
        compiles, its scans, and a BMI-only variant's compiles -- may outgrow
        a command line, one directory per linked provider. Forced past the
        threshold, each goes through a response file and the build still
        builds, runs and is a no-op."""
        env = {'MESON_RSP_THRESHOLD': '0'}
        self.init(os.path.join(self.unit_test_dir, '166 bmi classes'),
                  extra_args=list(extra_args), override_envvars=env)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            used = {m for m in re.findall(r'^build [^\n]*?: (\S+_RSP)\b', f.read(), re.M)}
        for kind in ('MODULE_COMPILER', 'MODULE_SCAN', 'BMI_VARIANT'):
            self.assertTrue(any(kind in r for r in used), f'no {kind} edge uses a response file: {used}')
        self.build(override_envvars={**self.NO_CCACHE, **env})
        self.run_tests()
        self.assertBuildIsNoop()

    def check_import_std_reuse(self, extra_args: T.Sequence[str] = ()) -> None:
        """Every consumer of dependency('std') in one BMI class resolves the
        std target's one BMI; a divergent consumer gets the single variant."""
        self.build_and_check_modules('220 import std reuse', extra_args=extra_args)
        std = '__meson_cxx_std'
        root = self.bmi_root(std)
        class_dirs = self.bmi_class_dirs(std)
        self.assertEqual(len(class_dirs), 2, f'expected two BMI class dirs, got {class_dirs}')
        for d in class_dirs:
            self.assertTrue(os.path.isfile(os.path.join(root, d, self._bmi_file('std'))))
        self.assertEqual(len(self.bmi_variant_ids()), 1, 'expected exactly one BMI variant of std')
        progs = ('proga', 'progb', 'progc', 'progdiv')
        std_lib = os.path.basename(self.target_output(std))
        for p in progs:
            self.assertEqual(self.count_on_link_line(self.target_output(p), std_lib), 1,
                             f'{p} must link the std library exactly once')
            self.assert_resolves_from(p, 'std', std)
        same = {self.resolved_bmi(p, 'std') for p in progs[:3]}
        self.assertEqual(len(same), 1, f'same-class consumers must share one std BMI, got {same}')
        self.assertNotIn(self.resolved_bmi('progdiv', 'std'), same)

    def check_gcc_module_mappers(self) -> None:
        """GCC builds resolve modules through Meson's mapper program, whatever
        their class topology: every scan, header-unit and compile edge spawns
        it, the compile alone with its own per-TU table (which the collate
        writes), and every manifest and table it names exists. A scan names no
        table: it runs before the collate that writes one."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        rule = out = ''
        counts = {'scan': 0, 'unit': 0, 'compile': 0}

        def program_args(text: str, kind: str) -> str:
            text = self._program_mapper_flag(text)
            self.assertIn('-fmodule-mapper=|', text, f'{kind} edge for {out} must ask the mapper program')
            units = self._program_mapper_word(text, '--units')
            self.assertIsNotNone(units, f'{kind} edge for {out} names no units manifest')
            self.assertTrue(os.path.isfile(os.path.join(self.builddir, units)),
                            f'missing units manifest {units}')
            return text

        for line in lines:
            if line.startswith('build '):
                head, _, tail = line.partition(': ')
                out = head[len('build '):].split(' ')[0]
                rule = tail.split(' ')[0]
                continue
            s = line.strip()
            if 'MODULE_SCAN' in rule and s.startswith('ARGS ='):
                counts['scan'] += 1
                self.assertNotIn('--table', program_args(s, 'scan'),
                                 f'scan edge for {out} runs before any table exists')
            elif 'HEADER_UNIT' in rule and s.startswith('HUMAPPER ='):
                counts['unit'] += 1
                program_args(s, 'header-unit')
            elif rule.startswith('cpp_MODULE_COMPILER') and s.startswith('ARGS ='):
                counts['compile'] += 1
                # The table sits next to the object, or at a space-free digest
                # of its path where GCC cannot quote the mapper command line.
                table = self._program_mapper_word(program_args(s, 'compile'), '--table')
                self.assertIsNotNone(table, f'compile edge for {out} must answer from its own table')
                if self.host_cpp_compiler().program_mapper_quotes():
                    self.assertEqual(table.replace('\\', '/'),
                                     f'{out}.mapper.json'.replace('\\', '/'))
                self.assertTrue(os.path.isfile(os.path.join(self.builddir, table)),
                                f'missing table {table} for {out}')
        self.assertGreater(counts['compile'], 0, 'no module compile edges found')

    @staticmethod
    def _program_mapper_flag(text: str) -> str:
        # The mapper flag is one shell-quoted word in ARGS; undo ninja's '$ '
        # escape so its own words read plainly.
        return text.replace('$ ', ' ')

    @staticmethod
    def _program_mapper_word(text: str, option: str) -> T.Optional[str]:
        """The value of `option` (--units, --table) in a mapper flag, with the
        backslash escapes GCC's command-line splitter removes undone: a
        Windows path arrives with every backslash doubled."""
        m = re.search(re.escape(option) + r' ((?:\\.|[^\s\\?])+)', text)
        return re.sub(r'\\(.)', r'\1', m.group(1)) if m else None

    def bmi_class_digests(self) -> T.Set[str]:
        """The distinct BMI-class digests of the per-target public BMI
        directories build.ninja names: one per BMI class in use."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            return set(re.findall(r'@bmi-public[/\\]([0-9a-f]{12})', f.read()))

    def module_compile_args(self) -> T.Dict[str, str]:
        """Module compile output -> its ARGS line, as build.ninja has it."""
        found: T.Dict[str, str] = {}
        out = rule = ''
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build '):
                    head, _, tail = line.partition(': ')
                    out = head[len('build '):].split(' ')[0]
                    rule = tail.split(' ')[0]
                elif rule.startswith('cpp_MODULE_COMPILER') and line.strip().startswith('ARGS ='):
                    found[out] = line
        return found

    def gcc_program_tables(self) -> T.Dict[str, str]:
        """Compile output -> the mapper program table it answers from, as the
        compile edges in build.ninja name them."""
        tables: T.Dict[str, str] = {}
        out = rule = ''
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build '):
                    head, _, tail = line.partition(': ')
                    out = head[len('build '):].split(' ')[0]
                    rule = tail.split(' ')[0]
                elif rule.startswith('cpp_MODULE_COMPILER') and line.strip().startswith('ARGS ='):
                    table = self._program_mapper_word(self._program_mapper_flag(line), '--table')
                    if table is not None:
                        tables[out] = table
        return tables

    # --- Header-unit outcomes on GCC: each TU's table maps the header's real
    # path to a BMI a unit edge wrote.

    def assert_gcc_table_units(self, target: str, basename: str, count: int = 1) -> T.Set[str]:
        """`target`'s tables map exactly `count` real path(s) ending in
        `basename` to a header-unit BMI, each one an output of a unit edge and
        on disk. Returns the BMIs."""
        bmis = self.header_unit_bmis_of(target, basename)
        self.assertEqual(len(bmis), count, f'{target}: {basename} BMIs {bmis}')
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            unit_outs = {line[len('build '):].partition(':')[0].strip()
                         for line in f if line.startswith('build ') and 'HEADER_UNIT' in line}
        for bmi in bmis:
            self.assertIn(bmi, unit_outs, f'{bmi} is no header-unit edge output')
            self.assertTrue(os.path.isfile(os.path.join(self.builddir, bmi)), bmi)
        return bmis

    def gcc_table_unit_paths(self, target: str) -> T.Set[str]:
        """Every header-unit real path `target`'s tables map."""
        matches = [m for pat in (f'{target}.p', f'{target}.*.p')
                   for m in glob.glob(os.path.join(self.builddir, pat))]
        self.assertEqual(len(matches), 1,
                         f'expected one private dir for {target}, got {matches}')
        rel = os.path.relpath(matches[0], self.builddir).replace('\\', '/') + '/'
        paths: T.Set[str] = set()
        for out, table in self.gcc_program_tables().items():
            if out.startswith(rel):
                with open(os.path.join(self.builddir, table), encoding='utf-8') as f:
                    paths.update(json.load(f)['header_units'])
        return paths
