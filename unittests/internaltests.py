# SPDX-License-Identifier: Apache-2.0
# Copyright 2016-2021 The Meson development team

from configparser import ConfigParser
from pathlib import Path
from unittest import mock
import argparse
import contextlib
import io
import json
import operator
import os
import pickle
import stat
import subprocess
import tempfile
import textwrap
import typing as T
import unittest

import mesonbuild.mlog
import mesonbuild.depfile
import mesonbuild.dependencies.base
import mesonbuild.dependencies.factory
import mesonbuild.envconfig
import mesonbuild.environment
import mesonbuild.modules.gnome
import mesonbuild.scripts.env2mfile
from mesonbuild import coredata
from mesonbuild.compilers.c import ClangCCompiler, GnuCCompiler
from mesonbuild.compilers.compilers import ManyInOneLinkerOptionStyle
from mesonbuild.compilers.cpp import VisualStudioCPPCompiler
from mesonbuild.compilers.d import DmdDCompiler
from mesonbuild.compilers.detect import detect_c_compiler
from mesonbuild.compilers.mixins.visualstudio import MSVCCompiler, ClangClCompiler
from mesonbuild.linkers import linkers
from mesonbuild.interpreterbase import typed_pos_args, InvalidArguments, ObjectHolder
from mesonbuild.interpreterbase import typed_pos_args, InvalidArguments, typed_kwargs, ContainerTypeInfo, KwargInfo
from mesonbuild.mesonlib import (
    LibType, MachineChoice, PerMachine, SimpleABC, Version, is_windows, is_osx,
    is_cygwin, is_openbsd, search_version, MesonException, python_command,
    version_check_to_range,
)
from mesonbuild.options import OptionKey
from mesonbuild.interpreter.type_checking import in_set_validator, NoneType
from mesonbuild.dependencies.pkgconfig import PkgConfigDependency, PkgConfigInterface, PkgConfigCLI
from mesonbuild.programs import ExternalProgram
import mesonbuild.modules.pkgconfig
from mesonbuild import utils

from run_tests import get_fake_env, get_fake_options

from .helpers import *

class InternalTests(unittest.TestCase):

    def test_machine_info_is_ohos(self):
        def machine(system: str, subsystem: str) -> mesonbuild.envconfig.MachineInfo:
            return mesonbuild.envconfig.MachineInfo(
                system=system, cpu_family='aarch64', cpu='aarch64',
                endian='little', kernel='linux', subsystem=subsystem)

        # OHOS is modelled as an Android subsystem.
        ohos = machine('android', 'ohos')
        self.assertTrue(ohos.is_ohos())
        self.assertTrue(ohos.is_android())

        # Plain Android is not OHOS.
        self.assertFalse(machine('android', 'android').is_ohos())
        # A non-Android system with an 'ohos' subsystem is not OHOS either.
        self.assertFalse(machine('linux', 'ohos').is_ohos())

    def test_version_number(self):
        self.assertEqual(search_version('foobar 1.2.3'), '1.2.3')
        self.assertEqual(search_version('1.2.3'), '1.2.3')
        self.assertEqual(search_version('foobar 2016.10.28 1.2.3'), '1.2.3')
        self.assertEqual(search_version('2016.10.28 1.2.3'), '1.2.3')
        self.assertEqual(search_version('foobar 2016.10.128'), '2016.10.128')
        self.assertEqual(search_version('2016.10.128'), '2016.10.128')
        self.assertEqual(search_version('2016.10'), '2016.10')
        self.assertEqual(search_version('2016.10 1.2.3'), '1.2.3')
        self.assertEqual(search_version('oops v1.2.3'), '1.2.3')
        self.assertEqual(search_version('2016.oops 1.2.3'), '1.2.3')
        self.assertEqual(search_version('2016.x'), 'unknown version')
        self.assertEqual(search_version(r'something version is \033[32;2m1.2.0\033[0m.'), '1.2.0')

        # Literal output of mvn
        self.assertEqual(search_version(r'''\
            \033[1mApache Maven 3.8.1 (05c21c65bdfed0f71a2f2ada8b84da59348c4c5d)\033[0m
            Maven home: /nix/store/g84a9wnid2h1d3z2wfydy16dky73wh7i-apache-maven-3.8.1/maven
            Java version: 11.0.10, vendor: Oracle Corporation, runtime: /nix/store/afsnl4ahmm9svvl7s1a0cj41vw4nkmz4-openjdk-11.0.10+9/lib/openjdk
            Default locale: en_US, platform encoding: UTF-8
            OS name: "linux", version: "5.12.17", arch: "amd64", family: "unix"'''),
            '3.8.1')

    def test_module_bmi_naming_lockstep(self):
        # The BMI path is derived independently by the compiler
        # (module_name_to_filename) and the collator (depaccumulate.
        # module_to_filename, fed the compiler's dir/suffix); they must agree,
        # and must use forward slashes on every platform (they feed ninja paths
        # / dyndep).
        from mesonbuild.compilers.cpp import GnuCPPCompiler, VisualStudioCPPCompiler
        from mesonbuild.scripts.depaccumulate import module_to_filename
        cases = [
            (GnuCPPCompiler, {
                'foo': 'gcm.cache/foo.gcm',
                'pkg:part': 'gcm.cache/pkg-part.gcm',
                'my.module': 'gcm.cache/my.module.gcm',
            }),
            (VisualStudioCPPCompiler, {
                'foo': 'ifc.cache/foo.ifc',
                'pkg:part': 'ifc.cache/pkg-part.ifc',
                'my.module': 'ifc.cache/my.module.ifc',
            }),
        ]
        for cls, expected in cases:
            # These methods use no instance state, so an uninitialized instance
            # is enough to exercise the (self-dispatching) mapping.
            comp = cls.__new__(cls)
            bmidir = comp.get_module_cache_dir()
            suffix = comp.get_module_bmi_suffix()
            for name, want in expected.items():
                compiler_side = comp.module_name_to_filename(name)
                self.assertEqual(compiler_side, want)
                self.assertEqual(compiler_side, module_to_filename(name, bmidir, suffix))
                self.assertNotIn('\\', compiler_side)

    def test_depaccumulate_p1689_empty_ddis(self):
        # A C++-module-enabled target can have zero compiled C++ TUs (e.g. only
        # a C source plus a header, with cpp_modules: true), so the collate edge
        # is emitted with no .ddi inputs. run_p1689 must accept that and publish
        # an (empty) provided-module map rather than failing argument parsing.
        from mesonbuild.scripts.depaccumulate import run_p1689
        with tempfile.TemporaryDirectory() as d:
            dyndep = os.path.join(d, 'out.dd')
            provmap = os.path.join(d, 'provided-modules.json')
            rc = run_p1689(['--dyndep', dyndep, '--provmap', provmap,
                             '--bmi-dir', 'gcm.cache', '--bmi-suffix', '.gcm'])
            self.assertEqual(rc, 0)
            with open(provmap, encoding='utf-8') as f:
                self.assertEqual(json.load(f), {})
            with open(dyndep, encoding='utf-8') as f:
                self.assertIn('ninja_dyndep_version = 1', f.read())

    def test_depaccumulate_header_unit_mapline_join(self):
        # The GCC collate emits every declared name of a header unit's BMI into
        # each importer's mapper: a scan reports one name (an alias spelling)
        # while the compile asks the mapper for another (the real one), and a
        # mapper disables default naming outright, so a name missing from it is
        # a hard "no such module". A reverse map bmi -> [names] joins them; a
        # build with no --header-unit-bmi pairs is unchanged.
        from mesonbuild.scripts.depaccumulate import run_p1689
        from mesonbuild.utils.core import default_cmi_path

        def mapper_lines(reported, pairs):
            with tempfile.TemporaryDirectory() as d:
                obj = os.path.join(d, 'a.o')
                ddi = obj + '.ddi'
                with open(ddi, 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': obj, 'provides': [],
                                          'requires': [{'logical-name': reported}]}]}, f)
                argv = ['--dyndep', os.path.join(d, 'out.dd'),
                        '--provmap', os.path.join(d, 'pm.json'),
                        '--bmi-dir', 'gcm.cache', '--bmi-suffix', '.gcm',
                        '--mapper-suffix', '.mapper', '--default-cmi-root', 'gcm.cache']
                for name, bmi in pairs:
                    argv += ['--header-unit-bmi', name, bmi]
                argv.append(ddi)
                self.assertEqual(run_p1689(argv), 0)
                with open(obj + '.mapper', encoding='utf-8') as f:
                    return [ln for ln in f.read().splitlines() if 'util.h' in ln]

        # Two names bound to one BMI: a require reporting either name gets both
        # maplines, pointing at that BMI.
        bmi = default_cmi_path('./a/util.h', 'gcm.cache', '.gcm')
        pairs = [('./a/util.h', bmi), ('./b/util.h', bmi)]
        self.assertEqual(sorted(mapper_lines('./a/util.h', pairs)),
                         sorted([f'./a/util.h {bmi}', f'./b/util.h {bmi}']))
        self.assertEqual(sorted(mapper_lines('./b/util.h', pairs)),
                         sorted([f'./a/util.h {bmi}', f'./b/util.h {bmi}']))

        # No pairs: the scan-reported name reconstructs to its own default path,
        # and nothing joins in -- one mapline, unchanged from before the join.
        recon = default_cmi_path('./c/util.h', 'gcm.cache', '.gcm')
        self.assertEqual(mapper_lines('./c/util.h', []), [f'./c/util.h {recon}'])

    def test_supports_cpp_modules_p1689(self):
        # The P1689 pipeline (P1689 scan/collate, header units) needs
        # GCC >= 14 or MSVC >= 19.32; an older but modules-capable compiler
        # is gated out (module targets there fall back to the regex scan and
        # header units are unsupported). Clang is feature-probed rather than
        # version-gated: the gate is whether a P1689-capable clang-scan-deps
        # was found, so preset the lazy probe result.
        from mesonbuild.compilers.cpp import (
            GnuCPPCompiler, VisualStudioCPPCompiler, ClangCPPCompiler)

        def gate(cls, version):
            comp = cls.__new__(cls)
            comp.version = version
            return comp.supports_cpp_modules_p1689()

        self.assertFalse(gate(GnuCPPCompiler, '13.2.0'))
        self.assertTrue(gate(GnuCPPCompiler, '14.0.0'))
        self.assertFalse(gate(VisualStudioCPPCompiler, '19.31'))
        self.assertTrue(gate(VisualStudioCPPCompiler, '19.32'))

        clang = ClangCPPCompiler.__new__(ClangCPPCompiler)
        clang._clang_scan_deps = None
        self.assertFalse(clang.supports_cpp_modules_p1689())
        clang = ClangCPPCompiler.__new__(ClangCPPCompiler)
        clang._clang_scan_deps = '/usr/bin/clang-scan-deps'
        self.assertTrue(clang.supports_cpp_modules_p1689())

    def test_cpp_module_family_by_class(self):
        # The module pipeline dispatches on the toolchain family, resolved by
        # class so a new clang-derived id is covered by inheritance with no
        # per-site audit. clang-cl is not a Clang subclass and stays 'none';
        # xc32-gcc is GCC-based but deliberately kept out.
        from mesonbuild.compilers.cpp import (
            GnuCPPCompiler, VisualStudioCPPCompiler, ClangCPPCompiler,
            IntelLLVMCPPCompiler, ArmLtdClangCPPCompiler, EmscriptenCPPCompiler,
            AppleClangCPPCompiler, ClangClCPPCompiler, Xc32CPPCompiler)
        cases = {
            GnuCPPCompiler: 'gcc',
            VisualStudioCPPCompiler: 'msvc',
            ClangCPPCompiler: 'clang',
            IntelLLVMCPPCompiler: 'clang',
            ArmLtdClangCPPCompiler: 'clang',
            EmscriptenCPPCompiler: 'clang',
            AppleClangCPPCompiler: 'clang',
            ClangClCPPCompiler: 'none',
            Xc32CPPCompiler: 'none',
        }
        for cls, family in cases.items():
            self.assertEqual(cls.__new__(cls).cpp_module_family(), family, cls.__name__)

    def test_collate_depargs_shared_assembly(self):
        # A target's own collate and a BMI-only variant's collate both build
        # their depaccumulate flags through _collate_depargs; only the head
        # (--private-map vs --own-private-map) legitimately differs. Pin the
        # exact per-family tail so a flag added for one site cannot silently
        # drop from the other, and pin that the head is head-independent.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        be = NinjaBackend.__new__(NinjaBackend)
        be.build_to_src = '..'
        cpp = mock.MagicMock()
        cpp.get_module_bmi_suffix.return_value = '.gcm'
        cpp.get_module_cache_dir.return_value = 'gcm.cache'

        def depargs(family, head, *, harvests, interface_sources, header_units):
            cpp.cpp_module_family.return_value = family
            return be._collate_depargs(
                cpp, head, dep_provmaps=['dep/pm.json'],
                dep_private_maps=[('dep/priv.json', 'id0', 'disp0')],
                harvests=harvests, interface_sources=interface_sources,
                # A pair whose BMI is not its name's default path always emits.
                header_unit_bmis=[('ustd', 'gcm.cache/z.gcm')],
                header_units=header_units)

        deps = ['--dep-private-map', 'dep/priv.json', 'id0', 'disp0',
                '--dep-provmap', 'dep/pm.json']
        # GCC target: no harvest stamp, the mapper/CMI flags, no interface list.
        self.assertEqual(
            depargs('gcc', ['--private-map', 'p.json', 'd'],
                    harvests=False, interface_sources=[], header_units=[]),
            ['--private-map', 'p.json', 'd'] + deps
            + ['--mapper-suffix', '.mapper', '--default-cmi-root', 'gcm.cache',
               '--header-unit-bmi', 'ustd', 'gcm.cache/z.gcm'])
        # GCC variant: always harvests, so the mapper block *and* the recompiled
        # interfaces, the interface list coming after the mapper block.
        self.assertEqual(
            depargs('gcc', ['--own-private-map', 'p.json', 'd'],
                    harvests=True, interface_sources=['x.cc'], header_units=[]),
            ['--own-private-map', 'p.json', 'd'] + deps
            + ['--stamp-suffix', '.gcm.stamp',
               '--mapper-suffix', '.mapper', '--default-cmi-root', 'gcm.cache',
               '--header-unit-bmi', 'ustd', 'gcm.cache/z.gcm',
               '--interface-source', 'x.cc'])
        # Clang: stamp plus the interface list, no mapper block.
        self.assertEqual(
            depargs('clang', ['--private-map', 'p.json', 'd'],
                    harvests=True, interface_sources=['x.cc'], header_units=[]),
            ['--private-map', 'p.json', 'd'] + deps
            + ['--stamp-suffix', '.gcm.stamp', '--interface-source', 'x.cc'])
        # MSVC: the declared header units close the sequence. A non-empty list
        # exercises the --header-unit branch (build_to_src drives the parse);
        # header_units=[] on every case above pins it as a no-op there.
        self.assertEqual(
            depargs('msvc', ['--own-private-map', 'p.json', 'd'],
                    harvests=True, interface_sources=['x.cc'],
                    header_units=['<vec.h>', 'util.h']),
            ['--own-private-map', 'p.json', 'd'] + deps
            + ['--stamp-suffix', '.gcm.stamp', '--interface-source', 'x.cc',
               '--header-unit', 'system:vec.h', '--header-unit', 'user:util.h'])
        # The head is the only per-site difference: identical kwargs under the
        # two heads must agree on everything past it.
        for family in ('gcc', 'clang', 'msvc'):
            kw = dict(harvests=(family != 'gcc'),
                      interface_sources=[] if family == 'gcc' else ['x.cc'],
                      header_units=[])
            tgt = depargs(family, ['--private-map', 'p.json', 'd'], **kw)
            var = depargs(family, ['--own-private-map', 'p.json', 'd'], **kw)
            self.assertEqual(tgt[3:], var[3:], family)

    def _module_scanner_mock(self, family, *, uses_modules=True, version='19.40',
                             p1689=False, cpp_modules_args=None, extra_args=None):
        # A NinjaBackend and a mocked C++ target wired for the scanner-selection
        # and diagnostic paths. cpp_module_family is set explicitly: an unset
        # MagicMock compares unequal to every family string and would silently
        # flip the dispatch.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        from mesonbuild import build
        be = NinjaBackend.__new__(NinjaBackend)
        be.ninja_has_dyndeps = True
        cpp = mock.MagicMock()
        cpp.cpp_module_family.return_value = family
        cpp.get_id.return_value = {'gcc': 'gcc', 'msvc': 'msvc',
                                   'clang': 'intel-llvm', 'none': 'clang-cl'}[family]
        cpp.version = version
        cpp.supports_cpp_modules_p1689.return_value = p1689
        cpp.get_cpp_modules_args.return_value = cpp_modules_args or []
        target = mock.MagicMock(spec=build.BuildTarget)
        target.name = 't'
        target.compilers = {'cpp': cpp}
        target.uses_cpp_modules.return_value = uses_modules
        target.uses_fortran.return_value = False
        target.extra_args = {'cpp': extra_args or []}
        # These mocks stand for real module targets: they compile a C++ source
        # of their own and no Fortran source. The pure-linker case (no C++ TU) is
        # exercised separately in test_scanner_pure_c_consumer_not_blamed.
        target.has_cpp_source.return_value = True
        target.has_fortran_source.return_value = False
        return be, target

    def test_scanner_clang_family_selects_p1689(self):
        # A clang-derived compiler (id intel-llvm, family clang) with a working
        # clang-scan-deps must select the P1689 scanner and pass the diagnostic
        # -- dispatch is on the family, never the id.
        be, target = self._module_scanner_mock('clang', p1689=True)
        self.assertEqual(be.cpp_module_scanner_for_target(target), 'p1689')
        self.assertTrue(be.target_uses_p1689_cpp_modules(target))
        be.check_cpp_modules_scanner(target)  # must not raise

    def test_scanner_msvc_declared_modules_none_raises(self):
        # A declaring MSVC target that resolves to no scanner must error at
        # setup rather than fail at build time with raw cl errors.
        # (A) cl below the 19.28.28617 modules floor.
        be, target = self._module_scanner_mock('msvc', version='19.28.28000')
        with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.'
                        'current_vs_supports_modules', return_value=True):
            self.assertEqual(be.cpp_module_scanner_for_target(target), 'none')
            with self.assertRaises(MesonException):
                be.check_cpp_modules_scanner(target)
        # (B) a too-old developer prompt, cl new enough otherwise.
        be, target = self._module_scanner_mock('msvc', version='19.40')
        with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.'
                        'current_vs_supports_modules', return_value=False):
            self.assertEqual(be.cpp_module_scanner_for_target(target), 'none')
            with self.assertRaises(MesonException):
                be.check_cpp_modules_scanner(target)
        # (C) the documented regex fallback (cl 19.28.28617 - 19.31) is fine.
        be, target = self._module_scanner_mock('msvc', version='19.29')
        with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.'
                        'current_vs_supports_modules', return_value=True):
            self.assertEqual(be.cpp_module_scanner_for_target(target), 'regex')
            be.check_cpp_modules_scanner(target)  # must not raise

    def test_scanner_unsupported_family_declared_modules_raises(self):
        # A declaring target on a compiler outside the pipeline (clang-cl,
        # family 'none') errors at setup: its modules would be compiled as
        # plain C++ with no scanning. The bare-modules-flag escape hatch still
        # answers 'regex', and a non-declaring target is never asked.
        be, target = self._module_scanner_mock(
            'none', cpp_modules_args=['-fmodules', '-fmodules-ts'])
        self.assertEqual(be.cpp_module_scanner_for_target(target), 'none')
        with self.assertRaises(MesonException):
            be.check_cpp_modules_scanner(target)
        be, target = self._module_scanner_mock(
            'none', cpp_modules_args=['-fmodules', '-fmodules-ts'],
            extra_args=['-fmodules'])
        self.assertEqual(be.cpp_module_scanner_for_target(target), 'regex')
        be.check_cpp_modules_scanner(target)  # must not raise
        be, target = self._module_scanner_mock('none', uses_modules=False)
        be.check_cpp_modules_scanner(target)  # must not raise

    def test_scanner_pure_c_consumer_not_blamed(self):
        # A pure-C target that merely links a C++ module provider carries a cpp
        # compiler (process_compilers adds it to pick the linker) and answers
        # uses_cpp_modules(), but it compiles no C++ source of its own. Even
        # where the family resolves to no scanner (Clang without clang-scan-deps
        # here), it must not be blamed for a missing tool it never needed.
        be, target = self._module_scanner_mock('clang', p1689=False)
        target.has_cpp_source.return_value = False
        self.assertEqual(be.cpp_module_scanner_for_target(target), 'none')
        be.check_cpp_modules_scanner(target)  # must not raise
        # A real C++ TU on that same dead-end compiler still raises.
        be, target = self._module_scanner_mock('clang', p1689=False)
        with self.assertRaises(MesonException):
            be.check_cpp_modules_scanner(target)

    def test_msvc_module_compile_args_use_cache_dir(self):
        # /ifcSearchDir and /ifcOutput must point at the compiler's own module
        # cache dir (get_module_cache_dir), not a hardcoded literal, so the BMI
        # search/output dir stays in lockstep with the collator's dir.
        comp = VisualStudioCPPCompiler.__new__(VisualStudioCPPCompiler)
        with mock.patch.object(comp, 'get_module_cache_dir', return_value='sentinel.cache'):
            args = comp.get_module_compile_args()
        self.assertNotIn('ifc.cache', args)
        self.assertNotIn('ifc.cache/', args)
        self.assertIn('sentinel.cache', args)
        self.assertIn('sentinel.cache/', args)

    def test_bmi_class_key(self):
        # The BMI equivalence-class key must be conservative: only allowlisted
        # (BMI-irrelevant) flag differences may hash equal; a difference in any
        # other flag -- including one the allowlist has never heard of -- must
        # split the class. Getting this wrong in the strict direction shares an
        # incompatible BMI; in the loose direction it only duplicates one.
        from mesonbuild.compilers.cpp import (
            GnuCPPCompiler, VisualStudioCPPCompiler, ClangCPPCompiler)

        gcc = GnuCPPCompiler.__new__(GnuCPPCompiler)
        clang = ClangCPPCompiler.__new__(ClangCPPCompiler)
        msvc = VisualStudioCPPCompiler.__new__(VisualStudioCPPCompiler)

        base = ['-std=c++20', '-DBAR', '-pthread']
        # Allowlisted differences do not split.
        self.assertEqual(gcc.get_bmi_class_key(base + ['-O2', '-Ia', '-Wall', '-fPIC']),
                         gcc.get_bmi_class_key(base + ['-O0', '-Ib', '-Werror']))
        # A family prefix ('W', 'O', 'w', 'g') is only safe as far as every
        # real flag sharing that leading letter is BMI-irrelevant. Each
        # assertion below is a regression lock for one flag `man gcc`/`clang
        # --help-hidden` shows sharing a leading letter with an allowlisted
        # family while meaning something unrelated and BMI-affecting.
        #
        # -Wa,/-Wp,/-Wl, forward opaque, comma-delimited content to another
        # tool stage -- -Wp, in particular can carry a real -D -- and must
        # not be swallowed by the bare 'W' meant for -Wall et al.
        self.assertNotEqual(gcc.get_bmi_class_key(base),
                            gcc.get_bmi_class_key(base + ['-Wp,-DFOO=1']))
        self.assertNotEqual(gcc.get_bmi_class_key(base),
                            gcc.get_bmi_class_key(base + ['-Wl,--as-needed']))
        self.assertNotEqual(gcc.get_bmi_class_key(base),
                            gcc.get_bmi_class_key(base + ['-Wa,--noexecstack']))
        self.assertNotEqual(clang.get_bmi_class_key(base),
                            clang.get_bmi_class_key(base + ['-Wp,-DFOO=1']))
        # -ObjC/-ObjC++ select a different source language, not an
        # optimization level, despite sharing 'O' with -O0../-O3.
        self.assertNotEqual(gcc.get_bmi_class_key(base),
                            gcc.get_bmi_class_key(base + ['-ObjC']))
        self.assertNotEqual(clang.get_bmi_class_key(base),
                            clang.get_bmi_class_key(base + ['-ObjC++']))
        # --gcc-toolchain= picks an entirely different GCC install
        # (headers/libs/macros), despite sharing 'g' with Clang's -g/-gdwarf/...
        self.assertNotEqual(clang.get_bmi_class_key(base),
                            clang.get_bmi_class_key(base + ['--gcc-toolchain=/opt/gcc-13']))
        # -working-directory changes how relative #includes resolve, despite
        # sharing 'w' with Clang's -w.
        self.assertNotEqual(clang.get_bmi_class_key(base),
                            clang.get_bmi_class_key(base + ['-working-directory', '/src']))
        # -wrapper forwards an opaque, comma-separated program+args list (the
        # same shape as -Wa,/-Wp,/-Wl,), despite sharing 'w' with GCC's -w.
        self.assertNotEqual(gcc.get_bmi_class_key(base),
                            gcc.get_bmi_class_key(base + ['-wrapper', 'gdb,--args']))
        # Ordinary -W warning flags that merely share the leading letter must
        # keep stripping: this must not regress into treating every -W* as
        # relevant.
        self.assertEqual(gcc.get_bmi_class_key(base + ['-Wpedantic']),
                         gcc.get_bmi_class_key(base))
        # Two-token and joined include spellings are both stripped.
        self.assertEqual(gcc.get_bmi_class_key(base + ['-isystem', '/x']),
                         gcc.get_bmi_class_key(base + ['-isystem/y']))
        # Every include-dir flag get_include_dir_flags knows -- not just -I and
        # -isystem -- is BMI-irrelevant: two targets differing only in an
        # -iquote or -idirafter directory share a class. Regression lock for the
        # class key splitting on an include dir the identity machinery already
        # resolves the same, which built a redundant BMI per split. Both the
        # two-token and the joined spelling strip (the joined form goes through
        # the prefix branch, the two-token form through the consuming branch).
        self.assertEqual(gcc.get_bmi_class_key(base + ['-iquote', '/a']),
                         gcc.get_bmi_class_key(base + ['-iquote', '/b']))
        self.assertEqual(gcc.get_bmi_class_key(base + ['-iquote/a']),
                         gcc.get_bmi_class_key(base))
        self.assertEqual(gcc.get_bmi_class_key(base + ['-idirafter', '/a']),
                         gcc.get_bmi_class_key(base + ['-idirafter', '/b']))
        self.assertEqual(clang.get_bmi_class_key(base + ['-iquote', '/a']),
                         clang.get_bmi_class_key(base))
        self.assertEqual(clang.get_bmi_class_key(base + ['-idirafter', '/a']),
                         clang.get_bmi_class_key(base))
        # Non-allowlisted differences split, unknown flags included.
        for divergent in (['-DFOO'], ['-std=c++23'], ['-ftrivial-auto-var-init=zero']):
            self.assertNotEqual(gcc.get_bmi_class_key(base),
                                gcc.get_bmi_class_key(base + divergent), divergent)
        self.assertNotEqual(gcc.get_bmi_class_key(['-std=c++20']),
                            gcc.get_bmi_class_key(['-std=c++20', '-pthread']))

        # Clang additionally strips -g and its module-machinery flags.
        self.assertEqual(clang.get_bmi_class_key(base + ['-g', '-fmodule-file=m=x.pcm']),
                         clang.get_bmi_class_key(base))
        self.assertNotEqual(clang.get_bmi_class_key(base),
                            clang.get_bmi_class_key(base + ['-DFOO']))

        # MSVC: warnings and BMI plumbing are stripped (both slash and dash
        # spellings), defines and /std: divergences split.
        mbase = ['/std:c++20', '/DBAR']
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['/W4', '-Ipath', '-isystem', '/x']),
                         msvc.get_bmi_class_key(mbase + ['/w']))
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['/reference', 'm=x.ifc']),
                         msvc.get_bmi_class_key(mbase))
        self.assertNotEqual(msvc.get_bmi_class_key(mbase),
                            msvc.get_bmi_class_key(mbase + ['/DFOO']))
        self.assertNotEqual(msvc.get_bmi_class_key(mbase),
                            msvc.get_bmi_class_key(['/std:c++23', '/DBAR']))
        # cl sees -iquote/-idirafter in unix form too (Meson emits them that way
        # until native conversion folds them into /I at write time), so they
        # strip on an msvc command line just as on a GNU one. Regression lock for
        # the observed cl defect: an -iquote dir passed via cpp_args leaking into
        # the class key and splitting same-file header-unit targets apart.
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['-iquote', '/a']),
                         msvc.get_bmi_class_key(mbase))
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['-idirafter', '/a']),
                         msvc.get_bmi_class_key(mbase))
        # The '/' lead is MSVC-only: a stray absolute path must survive on a
        # GNU-syntax command line even though 'W' would prefix-match it.
        self.assertIn('/Work/lib.a', gcc.get_bmi_class_key(base + ['/Work/lib.a']))

        # cpp_eh (/EH*) must split the class: 'E' is exact-only, not a
        # prefix, so it no longer swallows /EHsc, /EHs-c-, /EHa -- regression
        # lock for the bug where bare 'E' as a prefix merged exception-model
        # divergences into one class.
        self.assertNotEqual(msvc.get_bmi_class_key(mbase + ['/EHsc']),
                            msvc.get_bmi_class_key(mbase + ['/EHs-c-']))
        self.assertNotEqual(msvc.get_bmi_class_key(mbase + ['/EHsc']),
                            msvc.get_bmi_class_key(mbase + ['/EHa']))
        self.assertNotEqual(msvc.get_bmi_class_key(mbase),
                            msvc.get_bmi_class_key(mbase + ['/EHsc']))
        # Bare /E (preprocess-only) still strips as before -- the exact-set
        # move must not regress the original stripping intent.
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['/E']),
                         msvc.get_bmi_class_key(mbase))
        # Spot-check two more entries moved from prefix- to exact-matching:
        # still stripped when bare.
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['/nologo']),
                         msvc.get_bmi_class_key(mbase))
        self.assertEqual(msvc.get_bmi_class_key(mbase + ['/TP']),
                         msvc.get_bmi_class_key(mbase))

        # A suffix-spelled flag whose argument is a separate token strips as a
        # unit: neither the flag nor its argument reaches the key, so two
        # targets differing only in a prebuilt header unit or an external
        # include dir share a BMI class. The regression this locks: the bare
        # argument alone landing in the key (splitting the class on it, and --
        # worse -- being replayed without its flag).
        for pair in (['/headerUnit:quote', 'a.h=a.ifc'],
                     ['/headerUnit:angle', 'vector=vector.ifc'],
                     ['/headerUnit', 'a.h=a.ifc'],
                     ['/headerName:angle', 'vector'],
                     ['/external:I', 'C:/sdk/include'],
                     ['/sourceDependencies', 'dep.json'],
                     ['/ifcOutput', 'out/']):
            key = msvc.get_bmi_class_key(mbase + pair)
            self.assertEqual(key, msvc.get_bmi_class_key(mbase), pair)
            self.assertNotIn(pair[1], key)

        # Anti-drift tripwire: the BMI-class consuming set must cover every
        # include-dir flag get_include_dir_flags reports (dash-stripped), so the
        # class key and the header-unit identity machinery can never disagree on
        # what an include flag is. If this fails, the two inventories diverged --
        # get_bmi_irrelevant_args grew or lost an include flag get_include_dir_flags
        # did not, or vice versa; re-derive rather than re-hand-list.
        for comp in (gcc, clang, msvc):
            include_bodies = {flag.lstrip('-') for flag, _ in comp.get_include_dir_flags()}
            consuming = comp.get_bmi_irrelevant_args()[2]
            self.assertLessEqual(
                include_bodies, consuming,
                f'{type(comp).__name__}: get_bmi_irrelevant_args consuming set and '
                f'get_include_dir_flags inventories diverged (missing '
                f'{sorted(include_bodies - consuming)})')

    def test_split_bmi_args_keeps_detached_arguments_paired(self):
        # A flag and an argument of its own passed as a separate token must
        # land in the same half, adjacent and in order: the halves are
        # re-concatenated to replay a compile (the BMI-variant provider edge),
        # so a flag parted from its argument reaches the compiler as a bare,
        # meaningless token -- for cl, a nonexistent input file.
        from mesonbuild.compilers.cpp import GnuCPPCompiler, VisualStudioCPPCompiler

        gcc = GnuCPPCompiler.__new__(GnuCPPCompiler)
        msvc = VisualStudioCPPCompiler.__new__(VisualStudioCPPCompiler)

        def assert_paired(comp, args, expect_irrelevant: bool):
            relevant, irrelevant = comp.split_bmi_args(args)
            half = irrelevant if expect_irrelevant else relevant
            other = relevant if expect_irrelevant else irrelevant
            self.assertEqual(half, args, args)
            self.assertEqual(other, [], args)
            # And the replay concatenation keeps them adjacent, in order.
            replay = irrelevant + relevant
            self.assertEqual(replay[replay.index(args[0]) + 1], args[1], args)

        # cl's suffixed spellings take their argument as a separate token and
        # are enumerated as consuming, so the pair strips cleanly.
        for pair in (['/headerUnit:quote', 'a.h=a.ifc'],
                     ['/headerUnit:angle', 'vector=vector.ifc'],
                     ['/headerUnit', 'a.h=a.ifc'],
                     ['/headerName:quote', 'my.h'],
                     ['/headerName:angle', 'vector'],
                     ['/external:I', 'C:/sdk/include'],
                     ['/sourceDependencies', 'dep.json'],
                     ['/scanDependencies', 'scan.json'],
                     ['/ifcOutput', 'out/']):
            assert_paired(msvc, pair, expect_irrelevant=True)

        # An unenumerated suffixed spelling cannot be told apart from an
        # attached-argument one by shape, so a following token with no flag
        # lead is assumed to be its detached argument and the pair is kept
        # relevant: the class splits too eagerly (safe) rather than the flag
        # being emitted without its argument (broken).
        assert_paired(msvc, ['/headerUnit:future', 'a.h=a.ifc'], expect_irrelevant=False)
        assert_paired(msvc, ['/analyze:plugin', 'checks.dll'], expect_irrelevant=False)
        assert_paired(msvc, ['/Fd', 'out.pdb'], expect_irrelevant=False)

        # Attached spellings are unchanged: one token, nothing consumed.
        self.assertEqual(msvc.split_bmi_args(['/Foout.obj']), ([], ['/Foout.obj']))
        self.assertEqual(msvc.split_bmi_args(['/Foout.obj', '/W4']),
                         ([], ['/Foout.obj', '/W4']))
        self.assertEqual(msvc.split_bmi_args(['/external:W0', '/DFOO']),
                         (['/DFOO'], ['/external:W0']))
        self.assertEqual(gcc.split_bmi_args(['-Idir', '-Wall']), ([], ['-Idir', '-Wall']))

        # Exact-consuming flags still consume unconditionally.
        assert_paired(gcc, ['-isystem', '/x'], expect_irrelevant=True)
        assert_paired(msvc, ['/reference', 'm=x.ifc'], expect_irrelevant=True)

    def test_cpp_std_supports_modules(self):
        # C++ modules need C++20+. The helper must accept c++20 and later in all
        # spellings (c++/gnu++/vc++, draft aliases, latest) and reject older
        # standards, 'none' (compiler default), and unrecognized values.
        from mesonbuild.compilers.cpp import cpp_std_supports_modules
        for std in ('c++20', 'c++23', 'c++26', 'c++2a', 'c++2b', 'c++2c',
                    'gnu++20', 'gnu++23', 'gnu++26', 'gnu++2a', 'gnu++2b',
                    'gnu++2c', 'vc++20', 'vc++23', 'c++latest', 'vc++latest'):
            self.assertTrue(cpp_std_supports_modules(std), std)
        for std in ('c++98', 'c++03', 'c++11', 'c++14', 'c++17', 'c++1z',
                    'gnu++11', 'gnu++14', 'gnu++17', 'gnu++1z', 'vc++17',
                    'none', 'garbage'):
            self.assertFalse(cpp_std_supports_modules(std), std)

    def test_cpp_modules_require_cpp20(self):
        # A module-using target with cpp_std < c++20 must error during setup
        # rather than failing at build time in the compiler. The check runs in
        # the backend (generation time) because uses_cpp_modules() walks the
        # link graph and must not be queried before it is frozen.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        from mesonbuild.mesonlib import MesonException
        be = NinjaBackend.__new__(NinjaBackend)

        def check(std, uses_modules=True, has_cpp=True, has_cpp_source=True):
            be.get_target_option = mock.MagicMock(return_value=std)
            target = mock.MagicMock()
            target.for_machine = MachineChoice.HOST
            target.subproject = ''
            target.uses_cpp_modules.return_value = uses_modules
            target.has_cpp_source.return_value = has_cpp_source
            target.compilers = {'cpp': mock.MagicMock()} if has_cpp else {}
            be.check_cpp_modules_std(target)

        check('c++20')                          # ok
        check('c++17', uses_modules=False)      # not a module target: ignored
        check('c++17', has_cpp=False)           # no C++ compiler: ignored
        check('c++17', has_cpp_source=False)    # links a provider, no C++ TU: ignored
        with self.assertRaises(MesonException):
            check('c++17')
        with self.assertRaises(MesonException):
            check('none')

    def test_target_uses_p1689_cpp_modules_memoized(self):
        # The module-pipeline predicate is re-asked once per source on the
        # ninja-gen hot path; it must be memoized per target so the version
        # gate (and the rest of the stack) is evaluated only once.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        from mesonbuild import build
        be = NinjaBackend.__new__(NinjaBackend)
        be.ninja_has_dyndeps = True
        cpp = mock.MagicMock()
        cpp.get_id.return_value = 'gcc'
        cpp.cpp_module_family.return_value = 'gcc'
        cpp.version = '14.0.0'
        cpp.get_cpp_modules_args.return_value = []
        cpp.supports_cpp_modules_p1689.return_value = True
        target = mock.MagicMock(spec=build.BuildTarget)
        target.compilers = {'cpp': cpp}
        target.uses_cpp_modules.return_value = True
        # A Fortran target uses Fortran's scanner, not this pipeline; say so,
        # or the mock answers every unstubbed predicate with a truthy mock.
        target.uses_fortran.return_value = False
        target.has_fortran_source.return_value = False
        target.has_cpp_source.return_value = True
        target.extra_args = {'cpp': []}

        r1 = be.target_uses_p1689_cpp_modules(target)
        r2 = be.target_uses_p1689_cpp_modules(target)
        self.assertTrue(r1)
        self.assertEqual(r1, r2)
        # Second call is served from the cache: the version gate ran only once.
        self.assertEqual(cpp.supports_cpp_modules_p1689.call_count, 1)

    def test_should_use_dyndeps_msvc_version_floor(self):
        # VS 16.8/16.9 ship cl 19.28.x *below* build 28617 with broken modules,
        # so the dyndep gate must reject those (and honor
        # current_vs_supports_modules) rather than admitting any cl >= 19.28.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        from mesonbuild import build
        be = NinjaBackend.__new__(NinjaBackend)
        be.ninja_has_dyndeps = True

        def should(version, vs_ok=True):
            cpp = mock.MagicMock()
            cpp.get_id.return_value = 'msvc'
            cpp.cpp_module_family.return_value = 'msvc'
            cpp.version = version
            cpp.get_cpp_modules_args.return_value = []
            target = mock.MagicMock(spec=build.BuildTarget)
            target.compilers = {'cpp': cpp}
            target.uses_cpp_modules.return_value = True
            target.uses_fortran.return_value = False
            target.has_fortran_source.return_value = False
            target.has_cpp_source.return_value = True
            target.extra_args = {'cpp': []}
            with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.'
                            'current_vs_supports_modules', return_value=vs_ok):
                return be.should_use_dyndeps_for_target(target)

        self.assertFalse(should('19.28.28000'))  # pre-28617 build: broken
        self.assertTrue(should('19.28.28617'))   # first good build
        self.assertTrue(should('19.32.31114'))   # newer
        self.assertFalse(should('19.32.31114', vs_ok=False))  # old dev prompt

    def test_gcc_header_unit_rule_portable(self):
        # The GCC header-unit edge is a plain compiler invocation: $HUMAPPER
        # sends the CMI to the path the edge declares, so $out is a real BMI and
        # there is no stamping step to chain on. Ninja on Windows runs a rule's
        # command with no shell, so a '&&'-chained second step would not run.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        be = NinjaBackend.__new__(NinjaBackend)
        be.ninja = mock.MagicMock()
        be.ninja.has_rule.return_value = False
        be.environment = mock.MagicMock()
        be.environment.get_build_command.return_value = ['meson']
        be.get_compiler_rule_name = mock.MagicMock(return_value='cpp_HEADER_UNIT')
        captured: list = []
        be.add_rule = captured.append

        cpp = mock.MagicMock()
        cpp.get_id.return_value = 'gcc'
        cpp.cpp_module_family.return_value = 'gcc'
        cpp.for_machine = MachineChoice.HOST
        cpp.get_exelist.return_value = ['g++']
        cpp.get_module_compile_args.return_value = []
        cpp.get_dependency_gen_args.return_value = []
        be.generate_cpp_header_unit_rule(cpp)

        self.assertEqual(len(captured), 1)
        # Normalize quoting: ninja quotes every token on Windows but leaves plain
        # tokens bare on Unix, so match on the tokens rather than a
        # platform-specific quoting of them.
        command_str = captured[0].command_str.replace('"', '')
        self.assertNotIn('&&', command_str)
        self.assertNotIn('--internal', command_str)
        self.assertIn('$HUMAPPER', command_str)
        self.assertIn('-fmodule-only', command_str)

    def test_dir_alias_root_keying(self):
        # An alias root is a pure function of (real_dir, class_tag): the same
        # keying must land on the same root across calls (idempotent
        # regeneration), two tags over one directory must get two roots (that
        # is what gives two BMI classes two unit names), and a None tag must
        # produce exactly the legacy space-free path -- same digest input,
        # same imap/<12hex> shape -- so existing spaced-path builds regenerate
        # byte-identically.
        import hashlib
        from mesonbuild.backend.ninjabackend import NinjaBackend
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, 'build')
            real = os.path.join(d, 'src dir')
            os.makedirs(build)
            os.makedirs(real)
            be = NinjaBackend.__new__(NinjaBackend)
            be._dir_aliases = {}
            be._alias_root_real = {}
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = build

            legacy = be._dir_alias_root(real)
            if legacy is None:
                raise unittest.SkipTest('platform cannot make a directory link here')
            # The None keying is pinned to the digest of the directory alone.
            digest = hashlib.sha256(real.encode()).hexdigest()[:12]
            self.assertEqual(legacy, f'meson-private/imap/{digest}')

            tagged = be._dir_alias_root(real, 'aaaabbbbcccc')
            other = be._dir_alias_root(real, 'ddddeeeeffff')
            self.assertIsNotNone(tagged)
            self.assertIsNotNone(other)
            # Distinct roots per keying, all in the same imap namespace.
            self.assertEqual(len({legacy, tagged, other}), 3)
            for rel in (tagged, other):
                self.assertRegex(rel, r'^meson-private/imap/[0-9a-f]{12}$')
                # Each root reaches the same real directory.
                self.assertTrue(os.path.samefile(os.path.join(build, rel), real))
            # Same dir + same tag -> the same root, including across a fresh
            # backend (the on-disk link readback, not just the memo).
            self.assertEqual(be._dir_alias_root(real, 'aaaabbbbcccc'), tagged)
            be._dir_aliases = {}
            self.assertEqual(be._dir_alias_root(real, 'aaaabbbbcccc'), tagged)

    def test_prune_stale_dir_aliases(self):
        # At the end of generation, a directory link under meson-private/imap/
        # that this run did not (re)place must go -- a class rename or a
        # dropped divergence orphans the old digest's root -- but a plain
        # directory or file there, and the transient canary, must survive
        # untouched.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, 'build')
            real_a = os.path.join(d, 'a')
            os.makedirs(build)
            os.makedirs(real_a)

            be = NinjaBackend.__new__(NinjaBackend)
            be._dir_aliases = {}
            be._alias_root_real = {}
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = build

            # A root standing from a run before this one -- as though a
            # since-renamed class or a dropped divergence placed it.
            stale_user = be._dir_alias_root(real_a, 'deadbeefcafe')
            self.assertIsNotNone(stale_user)

            # This run's own bookkeeping starts empty (a fresh NinjaBackend,
            # as generate() gets) and places only a root over the same real
            # directory under a different tag -- a renamed class reaching
            # for the same header through its own root.
            be._dir_aliases = {}
            be._alias_root_real = {}
            live_user = be._dir_alias_root(real_a, 'aaaabbbbcccc')
            self.assertIsNotNone(live_user)
            self.assertNotEqual(live_user, stale_user)

            # A non-link entry under meson-private/imap/ (not an alias root)
            # must survive, and so must a canary somehow left on disk (the
            # real code always removes it itself; pruning must not reap it by
            # the ordinary stale-link rule).
            os.makedirs(os.path.join(build, 'meson-private', 'imap', 'not_a_link_dir'))
            canary = os.path.join(build, 'meson-private', 'imap', '.canary')
            os.symlink(build, canary)

            be._prune_stale_dir_aliases()

            priv_entries = os.listdir(os.path.join(build, 'meson-private', 'imap'))
            self.assertIn(os.path.basename(live_user), priv_entries)
            self.assertNotIn(os.path.basename(stale_user), priv_entries)
            self.assertIn('not_a_link_dir', priv_entries)
            self.assertIn('.canary', priv_entries)

    def test_header_unit_grammar_parse(self):
        # provision_header_units and generate_p1689_module_collate_target both
        # parse a declared header unit into (mode, spelling); they share
        # _parse_header_unit so the grammar is defined (and ordered) once,
        # rather than hand-parsed twice with the tuple in opposite orders.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        from mesonbuild.utils.universal import File
        parse = NinjaBackend._parse_header_unit
        # '<pkg/hdr.h>' is a system header; the angle brackets are stripped.
        self.assertEqual(parse('<pkg/hdr.h>', 'bld2src'), ('system', 'pkg/hdr.h'))
        # A plain string is a user header, spelled as written.
        self.assertEqual(parse('pkg/hdr.h', 'bld2src'), ('user', 'pkg/hdr.h'))
        # A File is a user header, spelled build-relative.
        f = File(False, 'sub', 'hdr.h')
        self.assertEqual(parse(f, 'bld2src'), ('user', f.rel_to_builddir('bld2src')))

    def test_header_probe_strips_forced_includes(self):
        # The -H probe asks where a header resolves, which the include search
        # path alone decides. A forced include cannot move that answer but can
        # hide it -- a header the prelude already pulled in is skipped by its own
        # guard and opens no file for -H to report -- so the probe drops
        # -include/-imacros, in both the spellings the compiler takes, and keeps
        # everything that does shape the search path.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        strip = NinjaBackend._without_forced_includes
        args = ['-std=c++20', '-include', 'a.h', '-I', 'inc', '-includeb.h',
                '-Iinc2', '-imacros', 'c.h', '-isystem', 'sys', '-imacrosd.h',
                '-isystemsys2', '-D', 'FOO', '-DBAR', 'plain']
        untouched = list(args)
        self.assertEqual(strip(args),
                         ['-std=c++20', '-I', 'inc', '-Iinc2', '-isystem', 'sys',
                          '-isystemsys2', '-D', 'FOO', '-DBAR', 'plain'])
        # The caller's list is its own: the probe strips a copy, and the args it
        # was handed go on to build the unit unchanged.
        self.assertEqual(args, untouched)
        # A flag whose argument never arrived must not take a token that is not
        # there (nor anything else with it).
        self.assertEqual(strip(['-Iinc', '-include']), ['-Iinc'])

    def test_header_probe_memo_ignores_forced_includes(self):
        # Two targets whose args differ only in their forced includes ask the
        # same question of the compiler, so they must share the one answer:
        # the memo is keyed on the stripped list, and the probe spawns once.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        be = NinjaBackend.__new__(NinjaBackend)
        be._probed_header_units = {}
        be.environment = mock.MagicMock()
        be.environment.get_build_dir.return_value = '/nonexistent'
        cpp = mock.MagicMock()
        cpp.get_id.return_value = 'gcc'
        cpp.cpp_module_family.return_value = 'gcc'
        cpp.get_exelist.return_value = ['g++']

        proc = mock.MagicMock()
        proc.returncode = 0
        stderr = '. /usr/include/c++/16/vector\n.. /usr/include/c++/16/bits/stl_algobase.h\n'
        with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.Popen_safe',
                        return_value=(proc, '', stderr)) as popen:
            first = be._probe_header_unit_path(
                cpp, ['-Iinc', '-include', 'prelude.h'], 'system', 'vector')
            second = be._probe_header_unit_path(
                cpp, ['-Iinc', '-imacrosother.h'], 'system', 'vector')

        self.assertEqual(first, '/usr/include/c++/16/vector')
        self.assertEqual(second, first)
        self.assertEqual(popen.call_count, 1)
        # And the compiler was asked without them: a prelude reaching the probed
        # header is exactly what would have made it answer nothing.
        cmd = popen.call_args[0][0]
        self.assertNotIn('-include', cmd)
        self.assertNotIn('prelude.h', cmd)
        self.assertNotIn('-imacrosother.h', cmd)
        self.assertIn('-Iinc', cmd)

    def test_gcc_include_chain_memo_keys_on_args(self):
        # The built-in chain is a pure function of the probe arglist, so the
        # memo keys on it: two targets whose args reshape the chain (--sysroot
        # here) each probe and cache their own. A coarser (machine, compiler)
        # key would hand the second target the first's chain and so alias the
        # wrong directories. Include-only differences reshape nothing, so they
        # over-split into identical chains -- extra spawns, never a wrong reuse.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        with tempfile.TemporaryDirectory() as builddir:
            # Real dirs: the chain drops any entry GCC lists but does not open.
            d1 = os.path.join(builddir, 'sys1')
            d2 = os.path.join(builddir, 'sys2')
            os.makedirs(d1)
            os.makedirs(d2)
            be = NinjaBackend.__new__(NinjaBackend)
            be._gcc_include_chains = {}
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = builddir
            cpp = mock.MagicMock()
            cpp.for_machine = MachineChoice.HOST
            cpp.get_id.return_value = 'gcc'
            cpp.get_exelist.return_value = ['g++']
            cpp.get_include_dir_flags.return_value = (
                ('-idirafter', None), ('-isystem', 3), ('-iquote', 1), ('-I', 2))
            proc = mock.MagicMock()
            proc.returncode = 0
            stderr = ('#include <...> search starts here:\n'
                      f' {d1}\n {d2}\nEnd of search list.\n')

            def probe(args):
                with mock.patch('mesonbuild.backend.ninjabackend.mesonlib.Popen_safe',
                                return_value=(proc, '', stderr)) as popen:
                    chain = be._gcc_include_chain(cpp, args)
                return chain, popen.call_count

            # A chain-shaping flag splits the memo: two probes, two entries.
            chain_a, calls_a = probe(['--sysroot=/a'])
            chain_b, calls_b = probe(['--sysroot=/b'])
            self.assertEqual(calls_a, 1)
            self.assertEqual(calls_b, 1)
            self.assertEqual(len(be._gcc_include_chains), 2)
            # The same arglist reuses its entry: no second probe.
            _, calls_again = probe(['--sysroot=/a'])
            self.assertEqual(calls_again, 0)
            self.assertEqual(len(be._gcc_include_chains), 2)
            # Include-only differences reshape nothing: a fresh probe each (the
            # over-split), but byte-identical chains -- harmless by construction,
            # since a target's own include dirs are filtered out of the chain.
            chain_x, calls_x = probe(['-I/x'])
            chain_y, calls_y = probe(['-I/y'])
            self.assertEqual(calls_x, 1)
            self.assertEqual(calls_y, 1)
            self.assertEqual(chain_x, chain_y,
                             'an include-only arg difference must over-split into '
                             'identical chains, never diverge')
            # Every probe yields the built-in block, in search order.
            self.assertEqual(chain_a, [d1, d2])
            self.assertEqual(chain_a, chain_b)

    def test_header_unit_dedup_shared_by_spelling(self):
        # On a single-class machine (an empty BMI class registry) a header unit
        # is deduped globally by (mode, resolved identity) for both compilers:
        # another target that spells and resolves the same file reuses the one
        # BMI, even if its compile args differ, so the first edge builds the
        # unit for every consumer. A same-spelling unit resolving to a different
        # file is a different identity and earns its own BMI (see
        # test_header_unit_distinct_files_same_spelling). Per-class dedup on a
        # multi-class machine is covered by the bmi_classes fixture tests.
        from mesonbuild.backend.ninjabackend import NinjaBackend

        def outputs_for(cid, suffix, builddir):
            be = NinjaBackend.__new__(NinjaBackend)
            be.build_to_src = '..'
            be.all_outputs = set()
            be._bmi_classes = {}
            be._header_units = {}
            be._header_unit_class = {}
            be._header_unit_group = {}
            be._target_header_unit_outputs = {}
            be._target_header_unit_consumer_args = {}
            be._target_header_unit_bmis = {}
            be._target_generated_outputs = {}
            be._warned_header_unit_divergence = set()
            be._warned_header_unit_names = set()
            be._probed_header_units = {}
            be._dir_aliases = {}
            be._target_imported_header_units = {}
            # A real build tree holding the header on the -I path: GCC names a
            # unit by the path it resolves to, so a unit that resolves nowhere is
            # not built at all and has no output to dedup. The walk finds it here
            # without asking the compiler, so no probe runs.
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = builddir
            be.get_compiler_rule_name = mock.MagicMock(return_value='cpp_HEADER_UNIT')
            be.add_build = mock.MagicMock()
            cpp = mock.MagicMock()
            cpp.get_id.return_value = cid
            cpp.cpp_module_family.return_value = cid
            cpp.get_exelist.return_value = ['c++']
            cpp.get_module_bmi_suffix.return_value = suffix
            cpp.get_module_cache_dir.return_value = 'gcm.cache'
            cpp.get_header_unit_consumer_args.return_value = []
            # No provider imports units here: the target declares its own, so
            # the relevant/irrelevant split is only consulted for an empty
            # provider loop.
            cpp.split_bmi_args.return_value = ([], [])
            # The backend resolves a unit's identity through the compiler's
            # include-flag inventory; a real family shape drives the walk.
            cpp.get_include_dir_flags.return_value = (
                (('-idirafter', 0), ('-isystem', 0), ('-iquote', 0), ('-I', 0))
                if cid == 'msvc' else
                (('-cxx-isystem', 3), ('-idirafter', None), ('-isystem', 3),
                 ('-iquote', 1), ('-I', 2)))

            def provision(tid, args):
                target = mock.MagicMock()
                target.get_id.return_value = tid
                target.cpp_header_units = ['util.h']
                target.compilers = {'cpp': cpp}
                target.get_generated_sources.return_value = []
                be._generate_single_compile = mock.MagicMock(return_value=args)
                return be.provision_header_units(target, cpp)[0]

            return (provision('a', ['-Iinc', '-DFOO']),
                    provision('b', ['-Iinc', '-DBAR']),
                    provision('c', ['-Iinc', '-DFOO']))

        for cid, suffix in (('msvc', '.ifc'), ('gcc', '.gcm')):
            with tempfile.TemporaryDirectory() as builddir:
                os.makedirs(os.path.join(builddir, 'inc'))
                with open(os.path.join(builddir, 'inc', 'util.h'), 'w', encoding='utf-8') as f:
                    f.write('#pragma once\n')
                a, b, c = outputs_for(cid, suffix, builddir)
            # Differing args (-DFOO vs -DBAR) never split the BMI: one shared
            # edge per spelling, regardless of compiler.
            self.assertEqual(a, b)
            self.assertEqual(a, c)
            self.assertTrue(a.endswith(suffix))

    def test_header_unit_distinct_files_same_spelling(self):
        # Two targets in one class declare a header unit spelled alike but their
        # -I paths resolve it to different files. The dedup key carries the
        # resolved identity, so each earns its own BMI: a key of (mode, spelling)
        # alone would hand the second target the first's BMI, built from the
        # wrong header, silently. Identity is resolvable only for a user unit on
        # the -I path; a system unit falls back to its spelling, the sole dedup
        # axis there.
        from mesonbuild.backend.ninjabackend import NinjaBackend

        def outputs_for(cid, suffix, builddir):
            be = NinjaBackend.__new__(NinjaBackend)
            be.build_to_src = '..'
            be.all_outputs = set()
            be._bmi_classes = {}
            be._header_units = {}
            be._header_unit_class = {}
            be._header_unit_group = {}
            be._target_header_unit_outputs = {}
            be._target_header_unit_consumer_args = {}
            be._target_header_unit_bmis = {}
            be._target_generated_outputs = {}
            be._warned_header_unit_divergence = set()
            be._warned_header_unit_names = set()
            be._probed_header_units = {}
            be._dir_aliases = {}
            be._target_imported_header_units = {}
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = builddir
            be.get_compiler_rule_name = mock.MagicMock(return_value='cpp_HEADER_UNIT')
            be.add_build = mock.MagicMock()
            cpp = mock.MagicMock()
            cpp.get_id.return_value = cid
            cpp.cpp_module_family.return_value = cid
            cpp.get_exelist.return_value = ['c++']
            cpp.get_module_bmi_suffix.return_value = suffix
            cpp.get_module_cache_dir.return_value = 'gcm.cache'
            cpp.get_header_unit_consumer_args.return_value = []
            cpp.split_bmi_args.return_value = ([], [])
            # The backend resolves a unit's identity through the compiler's
            # include-flag inventory; a real family shape drives the walk.
            cpp.get_include_dir_flags.return_value = (
                (('-idirafter', 0), ('-isystem', 0), ('-iquote', 0), ('-I', 0))
                if cid == 'msvc' else
                (('-cxx-isystem', 3), ('-idirafter', None), ('-isystem', 3),
                 ('-iquote', 1), ('-I', 2)))

            def provision(tid, incdir):
                target = mock.MagicMock()
                target.get_id.return_value = tid
                target.cpp_header_units = ['util.h']
                target.compilers = {'cpp': cpp}
                target.get_generated_sources.return_value = []
                be._generate_single_compile = mock.MagicMock(return_value=[f'-I{incdir}'])
                return be.provision_header_units(target, cpp)[0]

            return provision('a', 'inc_a'), provision('b', 'inc_b')

        for cid, suffix in (('msvc', '.ifc'), ('gcc', '.gcm')):
            with tempfile.TemporaryDirectory() as builddir:
                for inc in ('inc_a', 'inc_b'):
                    os.makedirs(os.path.join(builddir, inc))
                    with open(os.path.join(builddir, inc, 'util.h'), 'w', encoding='utf-8') as f:
                        f.write(f'#pragma once\n// {inc}\n')
                a, b = outputs_for(cid, suffix, builddir)
            self.assertNotEqual(a, b, f'{cid}: two files sharing a spelling shared one BMI')
            self.assertTrue(a.endswith(suffix))
            self.assertTrue(b.endswith(suffix))

    def test_include_dir_flags_inventory(self):
        # The compiler owns the include-flag inventory the backend consumes.
        # GCC/Clang rank the quote-include search order (iquote < I < isystem,
        # and the C++-only cxx-isystem alongside isystem); -idirafter is
        # unranked, searched past the standard directories where a command-line
        # walk cannot follow. MSVC folds every spelling into /I, searched in
        # command-line order, so all share one rank.
        import types
        from mesonbuild.compilers.compilers import Compiler
        fn = Compiler.get_include_dir_flags
        gcc = fn(types.SimpleNamespace(get_argument_syntax=lambda: 'gcc'))
        msvc = fn(types.SimpleNamespace(get_argument_syntax=lambda: 'msvc'))
        self.assertEqual(dict(gcc), {'-cxx-isystem': 3, '-idirafter': None,
                                     '-isystem': 3, '-iquote': 1, '-I': 2})
        self.assertEqual(dict(msvc), {'-idirafter': 0, '-isystem': 0,
                                      '-iquote': 0, '-I': 0})

    def test_include_arg_parsers_consume_inventory(self):
        # The three include-arg parsers consume the compiler inventory. For -I
        # and -isystem alone their output is identical to the pre-inventory
        # hard-coded behavior (regression pin); -iquote/-idirafter/-cxx-isystem
        # are now recognized for extraction and rewriting too. Resolution walks
        # GCC's actual search order and skips -idirafter, whose directories are
        # searched past the standard ones a command-line walk cannot see.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        be = NinjaBackend.__new__(NinjaBackend)
        be._respell_dir = lambda d, class_tag=None, force_all=False: 'R:' + d
        be._reclass_dir = lambda d, new_tag: 'C:' + d
        cpp = mock.MagicMock()
        cpp.get_include_dir_flags.return_value = (
            ('-cxx-isystem', 3), ('-idirafter', None), ('-isystem', 3),
            ('-iquote', 1), ('-I', 2))

        base = ['-IjoinedA', '-I', 'sepA', '-isystem', 'sysSep',
                '-isystemJoinedS', '-DFOO', '-Wall']
        self.assertEqual(be._include_dirs_of(cpp, base),
                         ['joinedA', 'sepA', 'sysSep', 'JoinedS'])
        self.assertEqual(
            be._respell_include_args(cpp, list(base), 'tag'),
            ['-IR:joinedA', '-I', 'R:sepA', '-isystem', 'R:sysSep',
             '-isystemR:JoinedS', '-DFOO', '-Wall'])
        self.assertEqual(
            be._reclass_include_args(cpp, list(base), 'tag'),
            ['-IC:joinedA', '-I', 'C:sepA', '-isystem', 'C:sysSep',
             '-isystemC:JoinedS', '-DFOO', '-Wall'])

        new = ['-iquoteQ', '-idirafter', 'DA', '-cxx-isystem', 'CX']
        self.assertEqual(be._include_dirs_of(cpp, new), ['Q', 'DA', 'CX'])
        self.assertEqual(
            be._respell_include_args(cpp, list(new), 'tag'),
            ['-iquoteR:Q', '-idirafter', 'R:DA', '-cxx-isystem', 'R:CX'])
        self.assertEqual(
            be._reclass_include_args(cpp, list(new), 'tag'),
            ['-iquoteC:Q', '-idirafter', 'C:DA', '-cxx-isystem', 'C:CX'])

        with tempfile.TemporaryDirectory() as d:
            for sub in ('inc', 'q', 'da'):
                os.makedirs(os.path.join(d, sub))
                with open(os.path.join(d, sub, 'h.h'), 'w', encoding='utf-8') as f:
                    f.write('#pragma once\n')
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = d
            # -I precedes -iquote on the command line, but GCC searches -iquote
            # first, so the resolved key is the iquote path, not the -I one.
            self.assertEqual(
                be._header_unit_mapper_key(cpp, ['-Iinc', '-iquoteq'], 'h.h'),
                os.path.join('q', 'h.h'))
            # A file only under an -idirafter directory is left unresolved
            # (None) -- deferred to the compiler probe rather than trusting a
            # hit a standard directory could shadow unseen.
            self.assertIsNone(
                be._header_unit_mapper_key(cpp, ['-idirafterda'], 'h.h'))

    def test_cpp_header_units_rejected_on_unsupported_compiler(self):
        # Declaring cpp_header_units on a compiler without header-unit support
        # must error at configure time rather than silently dropping the units.
        # GCC, MSVC and Clang all build header units through the P1689
        # pipeline; anything else (or a pipeline-incapable toolchain) errors.
        from mesonbuild.interpreter.interpreter import Interpreter
        from mesonbuild.interpreterbase import InvalidArguments
        interp = Interpreter.__new__(Interpreter)

        def check(family, supported, cid=None, has_cpp=True):
            cpp = mock.MagicMock()
            cpp.supports_cpp_modules_p1689.return_value = supported
            cpp.cpp_module_family.return_value = family
            cpp.get_id.return_value = cid or family
            cpp.version = '18.0.0'
            target = mock.MagicMock()
            target.cpp_header_units = ['util.h']
            target.compilers = {'cpp': cpp} if has_cpp else {}
            interp._check_cpp_header_units_supported('t', target)

        check('gcc', True)   # supported compiler: no raise
        check('clang', True)  # P1689-capable clang builds header units too
        # A clang-derived compiler (family clang, distinct id) is accepted by
        # family, not by an id allowlist.
        check('clang', True, cid='intel-llvm')
        with self.assertRaises(InvalidArguments):
            check('gcc', False)  # modules-incapable gcc
        with self.assertRaises(InvalidArguments):
            check('clang', False)  # clang without a P1689 clang-scan-deps
        with self.assertRaises(InvalidArguments):
            check('none', True, cid='clang-cl')  # not a pipeline compiler
        with self.assertRaises(InvalidArguments):
            check('gcc', False, has_cpp=False)  # no C++ compiler at all

    def test_cpp_std_dependency_not_required_on_non_ninja_backend(self):
        # Synthesizing import std needs the Ninja backend. A required probe must
        # fail configuration on vs2022/xcode; a not-required probe must degrade
        # to not-found (and memoize it) rather than hard-error -- MSVC ships
        # modules.json, so the sources probe succeeds and the backend check is
        # what a graceful optional probe hits there.
        from mesonbuild.interpreter.interpreter import Interpreter
        from mesonbuild.interpreterbase import InterpreterException

        for_machine = MachineChoice.HOST

        def make_interp(backend_name):
            interp = Interpreter.__new__(Interpreter)
            interp.subproject = ''
            interp.environment = mock.MagicMock()
            interp.project_version = '1'
            cpp = mock.MagicMock()
            cpp.get_std_module_sources.return_value = {'std': '/std.cc'}
            cpp.get_std_module_extra_args.return_value = []
            interp.compilers = {for_machine: {'cpp': cpp}}
            interp.coredata = mock.MagicMock()
            interp.coredata.get_external_args.return_value = []
            interp.build = mock.MagicMock()
            interp.build.dependency_overrides = {for_machine: {}}
            interp.build.cpp_std_module_deps = {for_machine: {}}
            interp.build.global_args = {for_machine: {}}
            interp.current_build_project = mock.MagicMock(
                return_value=mock.MagicMock(project_args={for_machine: {}}))
            interp.backend = mock.MagicMock()
            interp.backend.name = backend_name
            return interp

        def call(interp, required):
            kwargs = {'native': for_machine, 'required': required}
            return interp._cpp_std_module_dependency(mock.MagicMock(), kwargs, threads=False)

        # (a) required: false + non-ninja + sources found -> not-found, memoized.
        interp = make_interp('vs2022')
        dep = call(interp, False)
        self.assertFalse(dep.found())
        memo = interp.build.cpp_std_module_deps[for_machine]
        self.assertEqual(list(memo.values()), [dep])
        # A second identical call re-probes (the memo key is derived from the
        # probe result; the compiler-level caches make that cheap) and returns
        # the very same memoized object.
        dep2 = call(interp, False)
        self.assertIs(dep2, dep)
        self.assertEqual(
            interp.compilers[for_machine]['cpp'].get_std_module_sources.call_count, 2)

        # (b) required: true (the default) + non-ninja -> the backend raise.
        interp = make_interp('vs2022')
        with self.assertRaises(InterpreterException) as cm:
            interp._cpp_std_module_dependency(
                mock.MagicMock(), {'native': for_machine}, threads=False)
        self.assertIn('Ninja backend', str(cm.exception))

        # (c) ninja backend: the backend check does not fire; synthesis proceeds
        # (func_static_lib stubbed) and a found() dependency comes back.
        interp = make_interp('ninja')
        interp.func_static_lib = mock.MagicMock(return_value=mock.MagicMock())
        dep = call(interp, False)
        self.assertTrue(dep.found())

    def test_depaccumulate_p1689_missing_module_hint(self):
        # A module required by no provider in the build is a build-time error;
        # for a non-std module the message must point at how to declare the
        # provider (cpp_modules: true), so a user exporting a module the
        # non-canonical way is not left with a bare "provided by no target".
        from mesonbuild.scripts.depaccumulate import run_p1689
        from mesonbuild.utils.core import MesonException
        with tempfile.TemporaryDirectory() as d:
            ddi = os.path.join(d, 'main.cpp.o.ddi')
            with open(ddi, 'w', encoding='utf-8') as f:
                json.dump({'rules': [{'primary-output': 'main.cpp.o',
                                      'requires': [{'logical-name': 'mod'}]}]}, f)
            with self.assertRaises(MesonException) as cm:
                run_p1689(['--dyndep', os.path.join(d, 'out.dd'),
                            '--provmap', os.path.join(d, 'pm.json'),
                            '--bmi-dir', 'gcm.cache', '--bmi-suffix', '.gcm', ddi])
            msg = str(cm.exception)
            self.assertIn('provided by no target', msg)
            self.assertIn('cpp_modules: true', msg)

    def test_depaccumulate_p1689_duplicate_provider_across_targets(self):
        # Two unrelated targets exporting the same module never meet in one
        # collate, but their BMIs share one cache path keyed by the module
        # name; the second collate must error via the on-disk owner claim
        # rather than leave ninja silently wedged on the colliding BMI.
        from mesonbuild.scripts.depaccumulate import run_p1689
        from mesonbuild.utils.core import MesonException

        def collate(d: str, bmidir: str, tgt: str) -> int:
            ddi = os.path.join(d, f'{tgt}.cpp.o.ddi')
            with open(ddi, 'w', encoding='utf-8') as f:
                json.dump({'rules': [{'primary-output': f'{tgt}.cpp.o',
                                      'provides': [{'logical-name': 'dupmod'}]}]}, f)
            return run_p1689(['--dyndep', os.path.join(d, f'{tgt}.dd'),
                              '--provmap', os.path.join(d, f'{tgt}-pm.json'),
                              '--bmi-dir', bmidir, '--bmi-suffix', '.gcm', ddi])

        with tempfile.TemporaryDirectory() as d:
            bmidir = os.path.join(d, 'gcm.cache')
            self.assertEqual(collate(d, bmidir, 'liba'), 0)
            # Re-collating the same target (an ordinary rebuild) is no duplicate.
            self.assertEqual(collate(d, bmidir, 'liba'), 0)
            with self.assertRaises(MesonException) as cm:
                collate(d, bmidir, 'libb')
            msg = str(cm.exception)
            self.assertIn('exported by more than one target', msg)
            self.assertIn('dupmod', msg)
            # A claim always appears with its contents (published by hard
            # link): an owner file that exists but is empty is not a live
            # claim -- the artifact of the pre-atomic write scheme that let
            # two concurrent collates both take ownership -- and must be
            # taken over rather than trusted.
            owner = os.path.join(bmidir, 'dupmod.gcm.owner')
            os.unlink(owner)
            with open(owner, 'w', encoding='utf-8'):
                pass
            self.assertEqual(collate(d, bmidir, 'libb'), 0)
            with open(owner, encoding='utf-8') as f:
                self.assertTrue(f.read().endswith('libb-pm.json'))
            # A stale claim heals: once the owner's map no longer lists the
            # module (it moved away and the old provider re-collated), another
            # target may take the name over ...
            with open(os.path.join(d, 'liba-pm.json'), 'w', encoding='utf-8') as f:
                json.dump({}, f)
            self.assertEqual(collate(d, bmidir, 'libb'), 0)
            # ... after which the original owner is the refused stranger.
            with self.assertRaises(MesonException):
                collate(d, bmidir, 'liba')

    def test_depaccumulate_claim_owner_vanishes_under_a_loser(self):
        # Collates run concurrently under ninja, and a stale claim is taken
        # over by unlinking its owner file: a claimant that loses the atomic
        # link can therefore find the very claim it lost to already gone by
        # the time it reads it. That means the name is unowned again, not that
        # the build is broken -- the claim must be retried, not raised on.
        # Driven at the seam, the window itself being unreproducible: the
        # first os.link fails the way a lost race fails, having first removed
        # the owner file the way a concurrent takeover would.
        from mesonbuild.scripts.depaccumulate import _claim_module_provider

        real_link = os.link

        with tempfile.TemporaryDirectory() as d:
            cache_bmi = os.path.join(d, 'gcm.cache', 'mod.gcm')
            owner_file = cache_bmi + '.owner'
            ours = os.path.join(d, 'libb-pm.json')
            theirs = os.path.join(d, 'liba-pm.json')
            os.mkdir(os.path.dirname(cache_bmi))
            with open(theirs, 'w', encoding='utf-8') as f:
                json.dump({'mod': 'gcm.cache/mod.gcm'}, f)
            with open(owner_file, 'w', encoding='utf-8') as f:
                f.write(theirs)

            calls: T.List[None] = []

            def link(src: str, dst: str) -> None:
                calls.append(None)
                if len(calls) == 1:
                    # The claim we lose to is taken over and unlinked by a
                    # third collate in the same instant.
                    os.unlink(owner_file)
                    raise FileExistsError(f'File exists: {dst!r}')
                real_link(src, dst)

            with mock.patch('os.link', link):
                _claim_module_provider('mod', cache_bmi, ours)
            # Retried rather than trusting the failed link's verdict, and the
            # claim is now ours.
            self.assertGreater(len(calls), 1)
            with open(owner_file, encoding='utf-8') as f:
                self.assertEqual(f.read(), ours)
            # No temp file left behind.
            self.assertEqual(os.listdir(os.path.dirname(cache_bmi)),
                             [os.path.basename(owner_file)])

    def test_depaccumulate_p1689_interface_source_through_symlink(self):
        # A source declared as a module interface and the same source named in
        # a P1689 scan may be spelled by different routes to one file: a
        # scanner may canonicalize its source-path while the path Meson
        # derived host-side traverses a symlinked prefix (/usr/local ->
        # /opt/homebrew), which is exactly what the standard library's own
        # module source, found via -print-file-name, does. Either spelling
        # must satisfy the Clang interface check; a source that genuinely was
        # not declared must still be rejected.
        from mesonbuild.scripts.depaccumulate import run_p1689
        from mesonbuild.utils.core import MesonException

        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, 'real')
            link = os.path.join(d, 'link')
            os.mkdir(real)
            try:
                os.symlink(real, link, target_is_directory=True)
            except (OSError, NotImplementedError) as e:
                raise unittest.SkipTest(f'symlinks unavailable: {e}')
            for name in ('std.cc', 'other.cc'):
                with open(os.path.join(real, name), 'w', encoding='utf-8'):
                    pass

            def collate(src: str, declared: str) -> int:
                ddi = os.path.join(d, 'std.cc.o.ddi')
                with open(ddi, 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': 'std.cc.o',
                                          'provides': [{'logical-name': 'std',
                                                        'source-path': src}]}]}, f)
                return run_p1689(['--dyndep', os.path.join(d, 'out.dd'),
                                  '--provmap', os.path.join(d, 'pm.json'),
                                  '--bmi-dir', os.path.join(d, 'pcm.cache'),
                                  '--bmi-suffix', '.pcm',
                                  '--stamp-suffix', '.pcm.stamp',
                                  '--interface-source', declared, ddi])

            through_link = os.path.join(link, 'std.cc')
            through_real = os.path.join(real, 'std.cc')
            self.assertEqual(collate(through_link, through_real), 0)
            self.assertEqual(collate(through_real, through_link), 0)
            # Same file, two routes -- not "any file matches any declaration".
            with self.assertRaises(MesonException) as cm:
                collate(os.path.join(link, 'other.cc'), through_real)
            self.assertIn('not marked a module interface', str(cm.exception))

    def test_depaccumulate_p1689_private_bmi_dir(self):
        # --bmi-dir always names the shared class-cache directory, for both
        # this target's own public provides and anything reached through a
        # linked dependency. A module-providing executable's own module is
        # private (--all-provides-private): it resolves at --private-bmi-dir
        # instead, but a linked dependency's public module still resolves at
        # --bmi-dir. Without --all-provides-private/--private-bmi-dir every
        # name resolves at --bmi-dir, the old single-directory behaviour
        # (every pre-Stage-8 caller never passes them).
        from mesonbuild.scripts.depaccumulate import run_p1689

        def collate(extra: T.List[str]) -> int:
            return run_p1689(['--dyndep', 'out.dd', '--provmap', 'pm.json',
                              '--bmi-dir', 'shared', '--bmi-suffix', '.gcm',
                              '--mapper-suffix', '.mapper',
                              '--dep-provmap', 'dep-pm.json', *extra,
                              'test1.cppm.o.ddi', 'main1.cpp.o.ddi'])

        olddir = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open('test1.cppm.o.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': 'test1.cppm.o',
                                          'provides': [{'logical-name': 'tests'}]}]}, f)
                with open('main1.cpp.o.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': 'main1.cpp.o',
                                          'requires': [{'logical-name': 'tests'},
                                                       {'logical-name': 'libmod'}]}]}, f)
                with open('dep-pm.json', 'w', encoding='utf-8') as f:
                    json.dump({'libmod': 'shared/libmod.gcm'}, f)

                # Without --all-provides-private/--private-bmi-dir: both
                # names resolve at --bmi-dir, as before.
                self.assertEqual(collate([]), 0)
                with open('main1.cpp.o.mapper', encoding='utf-8') as f:
                    self.assertEqual(f.read(),
                                     'tests shared/tests.gcm\n'
                                     'libmod shared/libmod.gcm\n')

                # With --all-provides-private and --private-bmi-dir: the
                # own-provided name resolves at --private-bmi-dir, but the
                # dependency-provided name still resolves at --bmi-dir.
                self.assertEqual(collate(['--all-provides-private', '--private-bmi-dir', 'private']), 0)
                with open('main1.cpp.o.mapper', encoding='utf-8') as f:
                    self.assertEqual(f.read(),
                                     'tests private/tests.gcm\n'
                                     'libmod shared/libmod.gcm\n')
            finally:
                os.chdir(olddir)

    def test_depaccumulate_p1689_clang_interface_extension(self):
        # Clang publishes a module BMI only for sources the backend compiled
        # as interface units, decided by extension alone; a provider whose
        # source lacks the module extension would advertise a harvest stamp
        # nothing produces, and consumers would only fail later with the
        # compiler's "module 'x' not found". The collate must reject it,
        # naming the source and the fix. Sources declared via
        # cpp_module_interfaces (passed per source as --interface-source;
        # the std synthesis declares its bits/std.cc this way) and the
        # GCC/MSVC pipelines (no --stamp-suffix) are exempt.
        from mesonbuild.scripts.depaccumulate import run_p1689
        from mesonbuild.utils.core import MesonException

        def args(d: str, extra: T.List[str]) -> T.List[str]:
            return ['--dyndep', os.path.join(d, 'out.dd'),
                    '--provmap', os.path.join(d, 'pm.json'),
                    '--bmi-dir', os.path.join(d, 'pcm.cache'), '--bmi-suffix', '.pcm',
                    *extra, os.path.join(d, 'fmt.cc.o.ddi')]

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'fmt.cc.o.ddi'), 'w', encoding='utf-8') as f:
                json.dump({'rules': [{'primary-output': 'fmt.cc.o',
                                      'provides': [{'logical-name': 'fmt',
                                                    'is-interface': True,
                                                    'source-path': '../src/fmt.cc'}]}]}, f)
            with self.assertRaises(MesonException) as cm:
                run_p1689(args(d, ['--stamp-suffix', '.pcm.stamp']))
            msg = str(cm.exception)
            self.assertIn('../src/fmt.cc', msg)
            self.assertIn('.cppm', msg)
            self.assertEqual(run_p1689(args(d, ['--stamp-suffix', '.pcm.stamp',
                                                '--interface-source', '../src/fmt.cc'])), 0)
            self.assertEqual(run_p1689(args(d, [])), 0)

    def test_depaccumulate_p1689_mapper_emission(self):
        # With --mapper-suffix the collate writes one GCC module mapper per
        # TU (at the CWD-relative primary-output, like the compiler's own
        # BMI paths), naming exactly its provides and direct imports:
        # provides and named-module requires at the class-cache path,
        # header-unit requires at the compiler's default cache path
        # (--default-cmi-root; a mapper disables GCC's default mapping, so the
        # collate must reproduce it). A TU with no module traffic gets an
        # empty mapper, and no mapper is written at all without the flag.
        from mesonbuild.scripts.depaccumulate import run_p1689

        def collate(extra: T.List[str]) -> int:
            return run_p1689(['--dyndep', 'out.dd', '--provmap', 'pm.json',
                              '--bmi-dir', 'gcm.cache/deadbeefcafe',
                              '--bmi-suffix', '.gcm',
                              '--dep-provmap', 'dep-pm.json', *extra,
                              'a.cppm.o.ddi', 'main.cpp.o.ddi', 'plain.cpp.o.ddi'])

        def read(name: str) -> str:
            with open(name, encoding='utf-8') as f:
                return f.read()

        olddir = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open('a.cppm.o.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{
                        'primary-output': 'a.cppm.o',
                        'provides': [{'logical-name': 'A'}],
                        'requires': [{'logical-name': 'B'},
                                     {'logical-name': './util.h'}]}]}, f)
                with open('main.cpp.o.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': 'main.cpp.o',
                                          'requires': [{'logical-name': 'A'}]}]}, f)
                with open('plain.cpp.o.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{'primary-output': 'plain.cpp.o'}]}, f)
                with open('dep-pm.json', 'w', encoding='utf-8') as f:
                    json.dump({'B': 'gcm.cache/deadbeefcafe/B.gcm'}, f)

                self.assertEqual(collate([]), 0)
                for obj in ('a.cppm.o', 'main.cpp.o', 'plain.cpp.o'):
                    self.assertFalse(os.path.exists(obj + '.mapper'))

                self.assertEqual(collate(['--mapper-suffix', '.mapper',
                                          '--default-cmi-root', 'gcm.cache']), 0)
                self.assertEqual(read('a.cppm.o.mapper'),
                                 'A gcm.cache/deadbeefcafe/A.gcm\n'
                                 'B gcm.cache/deadbeefcafe/B.gcm\n'
                                 './util.h gcm.cache/,/util.h.gcm\n')
                self.assertEqual(read('main.cpp.o.mapper'),
                                 'A gcm.cache/deadbeefcafe/A.gcm\n')
                self.assertEqual(read('plain.cpp.o.mapper'), '')
            finally:
                os.chdir(olddir)

    def test_depaccumulate_p1689_mapper_stamp_mode(self):
        # A BMI-only variant collate (--stamp-suffix) must map a TU's export
        # to the edge's declared output (the primary-output the harvest then
        # publishes), while requires still map to the readable class-cache
        # BMI -- the provmap value there is an ordering stamp, not a BMI.
        from mesonbuild.scripts.depaccumulate import run_p1689
        olddir = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                os.mkdir('variant')
                with open('a.gcm.ddi', 'w', encoding='utf-8') as f:
                    json.dump({'rules': [{
                        'primary-output': 'variant/a.gcm',
                        'provides': [{'logical-name': 'A',
                                      'source-path': '../src/a.cppm'}],
                        'requires': [{'logical-name': 'B'}]}]}, f)
                with open('dep-pm.json', 'w', encoding='utf-8') as f:
                    json.dump({'B': 'other.p/b.cppm.o.gcm.stamp'}, f)
                self.assertEqual(run_p1689(
                    ['--dyndep', 'out.dd', '--provmap', 'pm.json',
                     '--bmi-dir', 'gcm.cache/deadbeefcafe', '--bmi-suffix', '.gcm',
                     '--dep-provmap', 'dep-pm.json',
                     '--stamp-suffix', '.gcm.stamp',
                     '--interface-source', '../src/a.cppm',
                     '--mapper-suffix', '.mapper',
                     '--default-cmi-root', 'gcm.cache', 'a.gcm.ddi']), 0)
                with open('variant/a.gcm.mapper', encoding='utf-8') as f:
                    self.assertEqual(f.read(),
                                     'A variant/a.gcm\n'
                                     'B gcm.cache/deadbeefcafe/B.gcm\n')
                with open('out.dd', encoding='utf-8') as f:
                    dd = f.read()
                # Ordering rides the dep's harvest stamp; no BMI output is
                # declared (the variant edge declares it statically).
                self.assertIn('other.p/b.cppm.o.gcm.stamp', dd)
                self.assertNotIn('deadbeefcafe/A.gcm', dd)
            finally:
                os.chdir(olddir)

    def test_depaccumulate_p1689_mapper_copy_if_different(self):
        # Mapper files are implicit inputs of compile edges: a re-collate that
        # does not change a TU's mapping must leave the file untouched
        # (mtime included), or every object in the target would recompile
        # whenever any module in it changes.
        from mesonbuild.scripts.depaccumulate import run_p1689

        def collate(requires: T.List[str]) -> int:
            with open('m.cpp.o.ddi', 'w', encoding='utf-8') as f:
                json.dump({'rules': [{
                    'primary-output': 'm.cpp.o',
                    'provides': [{'logical-name': 'M'}],
                    'requires': [{'logical-name': r} for r in requires]}]}, f)
            return run_p1689(['--dyndep', 'out.dd', '--provmap', 'pm.json',
                              '--bmi-dir', 'gcm.cache/deadbeefcafe',
                              '--bmi-suffix', '.gcm',
                              '--mapper-suffix', '.mapper',
                              '--default-cmi-root', 'gcm.cache', 'm.cpp.o.ddi'])

        olddir = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                self.assertEqual(collate([]), 0)
                before = os.stat('m.cpp.o.mapper').st_mtime_ns
                self.assertEqual(collate([]), 0)
                self.assertEqual(os.stat('m.cpp.o.mapper').st_mtime_ns, before,
                                 'unchanged mapper content must keep its mtime')
                self.assertEqual(collate(['./util.h']), 0)
                with open('m.cpp.o.mapper', encoding='utf-8') as f:
                    self.assertIn('./util.h gcm.cache/,/util.h.gcm', f.read())
            finally:
                os.chdir(olddir)

    def test_depaccumulate_default_cmi_path(self):
        # GCC's default header-unit CMI naming under the cache root: '.' and
        # '..' path components become ',' and ',,'; an absolute resolved path
        # is appended as-is under the cache root. A Windows drive letter's
        # colon is mangled the same way, to a hyphen -- confirmed against real
        # GCC's own "compiled module file is ..." diagnostic on WinLibs 16.
        from mesonbuild.utils.core import default_cmi_path
        cases = {
            './util.h': 'gcm.cache/,/util.h.gcm',
            './../srcx/hdr.h': 'gcm.cache/,/,,/srcx/hdr.h.gcm',
            '../srcx/hdr.h': 'gcm.cache/,,/srcx/hdr.h.gcm',
            '/usr/include/c++/16/vector': 'gcm.cache/usr/include/c++/16/vector.gcm',
            'C:/Users/x/vector': 'gcm.cache/C-/Users/x/vector.gcm',
        }
        for name, want in cases.items():
            self.assertEqual(default_cmi_path(name, 'gcm.cache', '.gcm'), want, name)

    def test_depaccumulate_is_header_unit(self):
        from mesonbuild.scripts.depaccumulate import _is_header_unit
        # cl tags a header-unit require with lookup-method include-quote/angle
        # and a named module with by-name.
        for method in ('include-quote', 'include-angle'):
            self.assertTrue(_is_header_unit({'logical-name': 'util.h', 'lookup-method': method}), method)
        self.assertFalse(_is_header_unit({'logical-name': 'mod', 'lookup-method': 'by-name'}), 'by-name')
        # GCC omits lookup-method, so fall back to shape: a header-unit require
        # is a resolved header path (POSIX or Windows); a named module or
        # ':partition' is an identifier with no path separator.
        for name in ('./util.h', '/usr/include/c++/16/vector', '../src/hdr.hpp',
                     '.\\util.h', 'C:\\proj\\util.h', 'C:/proj/util.h'):
            self.assertTrue(_is_header_unit({'logical-name': name}), name)
        for name in ('mod', 'mod:part', 'std', 'std.compat', 'my.module', 'pkg.sub:part'):
            self.assertFalse(_is_header_unit({'logical-name': name}), name)

    def test_simple_abc(self):
        from abc import abstractmethod

        # The whole point is for isinstance() to stay on the C fast path
        self.assertNotIn('__instancecheck__', vars(SimpleABC))
        self.assertNotIn('__subclasscheck__', vars(SimpleABC))

        class A(metaclass=SimpleABC):
            @abstractmethod
            def foo(self): ...

        class B(A, metaclass=SimpleABC):
            def foo(self):
                return 1

            @abstractmethod
            def bar(self): ...

        class C(B):
            def foo(self):
                return 2

        class D(B):
            def bar(self):
                return 3

        self.assertEqual(A.__abstractmethods__, frozenset({'foo'}))
        with self.assertRaises(TypeError):
            A()

        self.assertEqual(B.__abstractmethods__, frozenset({'bar'}))
        self.assertTrue(issubclass(B, A))
        with self.assertRaises(TypeError):
            B()

        self.assertEqual(C.__abstractmethods__, frozenset({'bar'}))
        self.assertTrue(issubclass(C, A))
        self.assertTrue(issubclass(C, B))
        with self.assertRaises(TypeError):
            # subclass inheriting SimpleABC
            C()

        self.assertEqual(D.__abstractmethods__, frozenset())
        self.assertTrue(issubclass(D, A))
        self.assertTrue(issubclass(D, B))
        self.assertTrue(issubclass(D, D))
        self.assertIsInstance(D(), A)
        self.assertIsInstance(D(), B)
        self.assertIsInstance(D(), D)

    def test_mode_symbolic_to_bits(self):
        modefunc = mesonbuild.mesonlib.FileMode.perms_s_to_bits
        self.assertEqual(modefunc('---------'), 0)
        self.assertEqual(modefunc('r--------'), stat.S_IRUSR)
        self.assertEqual(modefunc('---r-----'), stat.S_IRGRP)
        self.assertEqual(modefunc('------r--'), stat.S_IROTH)
        self.assertEqual(modefunc('-w-------'), stat.S_IWUSR)
        self.assertEqual(modefunc('----w----'), stat.S_IWGRP)
        self.assertEqual(modefunc('-------w-'), stat.S_IWOTH)
        self.assertEqual(modefunc('--x------'), stat.S_IXUSR)
        self.assertEqual(modefunc('-----x---'), stat.S_IXGRP)
        self.assertEqual(modefunc('--------x'), stat.S_IXOTH)
        self.assertEqual(modefunc('--S------'), stat.S_ISUID)
        self.assertEqual(modefunc('-----S---'), stat.S_ISGID)
        self.assertEqual(modefunc('--------T'), stat.S_ISVTX)
        self.assertEqual(modefunc('--s------'), stat.S_ISUID | stat.S_IXUSR)
        self.assertEqual(modefunc('-----s---'), stat.S_ISGID | stat.S_IXGRP)
        self.assertEqual(modefunc('--------t'), stat.S_ISVTX | stat.S_IXOTH)
        self.assertEqual(modefunc('rwx------'), stat.S_IRWXU)
        self.assertEqual(modefunc('---rwx---'), stat.S_IRWXG)
        self.assertEqual(modefunc('------rwx'), stat.S_IRWXO)
        # We could keep listing combinations exhaustively but that seems
        # tedious and pointless. Just test a few more.
        self.assertEqual(modefunc('rwxr-xr-x'),
                         stat.S_IRWXU |
                         stat.S_IRGRP | stat.S_IXGRP |
                         stat.S_IROTH | stat.S_IXOTH)
        self.assertEqual(modefunc('rw-r--r--'),
                         stat.S_IRUSR | stat.S_IWUSR |
                         stat.S_IRGRP |
                         stat.S_IROTH)
        self.assertEqual(modefunc('rwsr-x---'),
                         stat.S_IRWXU | stat.S_ISUID |
                         stat.S_IRGRP | stat.S_IXGRP)

    def test_compiler_args_class_none_flush(self):
        cc = ClangCCompiler([], [], 'fake', MachineChoice.HOST, get_fake_env())
        a = cc.compiler_args(['-I.'])
        #first we are checking if the tree construction deduplicates the correct -I argument
        a += ['-I..']
        a += ['-I./tests/']
        a += ['-I./tests2/']
        #think this here as assertion, we cannot apply it, otherwise the CompilerArgs would already flush the changes:
        # assertEqual(a, ['-I.', '-I./tests2/', '-I./tests/', '-I..', '-I.'])
        a += ['-I.']
        a += ['-I.', '-I./tests/']
        self.assertEqual(a, ['-I.', '-I./tests/', '-I./tests2/', '-I..'])

        #then we are checking that when CompilerArgs already have a build container list, that the deduplication is taking the correct one
        a += ['-I.', '-I./tests2/']
        self.assertEqual(a, ['-I.', '-I./tests2/', '-I./tests/', '-I..'])

    def test_compiler_args_class_d(self):
        d = DmdDCompiler([], 'fake', MachineChoice.HOST, get_fake_env(), 'arch')
        # check include order is kept when deduplicating
        a = d.compiler_args(['-Ifirst', '-Isecond', '-Ithird'])
        a += ['-Ifirst']
        self.assertEqual(a, ['-Ifirst', '-Isecond', '-Ithird'])

    def test_compiler_args_class_clike(self):
        cc = ClangCCompiler([], [], 'fake', MachineChoice.HOST, get_fake_env())
        # Test that empty initialization works
        a = cc.compiler_args()
        self.assertEqual(a, [])
        # Test that list initialization works
        a = cc.compiler_args(['-I.', '-I..'])
        self.assertEqual(a, ['-I.', '-I..'])
        # Test that there is no de-dup on initialization
        self.assertEqual(cc.compiler_args(['-I.', '-I.']), ['-I.', '-I.'])

        ## Test that appending works
        a.append('-I..')
        self.assertEqual(a, ['-I..', '-I.'])
        a.append('-O3')
        self.assertEqual(a, ['-I..', '-I.', '-O3'])

        ## Test that in-place addition works
        a += ['-O2', '-O2']
        self.assertEqual(a, ['-I..', '-I.', '-O3', '-O2', '-O2'])
        # Test that removal works
        a.remove('-O2')
        self.assertEqual(a, ['-I..', '-I.', '-O3', '-O2'])
        # Test that de-dup happens on addition
        a += ['-Ifoo', '-Ifoo']
        self.assertEqual(a, ['-Ifoo', '-I..', '-I.', '-O3', '-O2'])

        # .extend() is just +=, so we don't test it

        ## Test that addition works
        # Test that adding a list with just one old arg works and yields the same array
        a = a + ['-Ifoo']
        self.assertEqual(a, ['-Ifoo', '-I..', '-I.', '-O3', '-O2'])
        # Test that adding a list with one arg new and one old works
        a = a + ['-Ifoo', '-Ibaz']
        self.assertEqual(a, ['-Ifoo', '-Ibaz', '-I..', '-I.', '-O3', '-O2'])
        # Test that adding args that must be prepended and appended works
        a = a + ['-Ibar', '-Wall']
        self.assertEqual(a, ['-Ibar', '-Ifoo', '-Ibaz', '-I..', '-I.', '-O3', '-O2', '-Wall'])

        ## Test that reflected addition works
        # Test that adding to a list with just one old arg works and yields the same array
        a = ['-Ifoo'] + a
        self.assertEqual(a, ['-Ibar', '-Ifoo', '-Ibaz', '-I..', '-I.', '-O3', '-O2', '-Wall'])
        # Test that adding to a list with just one new arg that is not pre-pended works
        a = ['-Werror'] + a
        self.assertEqual(a, ['-Ibar', '-Ifoo', '-Ibaz', '-I..', '-I.', '-Werror', '-O3', '-O2', '-Wall'])
        # Test that adding to a list with two new args preserves the order
        a = ['-Ldir', '-Lbah'] + a
        self.assertEqual(a, ['-Ibar', '-Ifoo', '-Ibaz', '-I..', '-I.', '-Ldir', '-Lbah', '-Werror', '-O3', '-O2', '-Wall'])
        # Test that adding to a list with old args does nothing
        a = ['-Ibar', '-Ibaz', '-Ifoo'] + a
        self.assertEqual(a, ['-Ibar', '-Ifoo', '-Ibaz', '-I..', '-I.', '-Ldir', '-Lbah', '-Werror', '-O3', '-O2', '-Wall'])

        ## Test that adding libraries works
        l = cc.compiler_args(['-Lfoodir', '-lfoo'])
        self.assertEqual(l, ['-Lfoodir', '-lfoo'])
        # Adding a library and a libpath appends both correctly
        l += ['-Lbardir', '-lbar']
        self.assertEqual(l, ['-Lbardir', '-Lfoodir', '-lfoo', '-lbar'])
        # Adding the same library again does nothing
        l += ['-lbar']
        self.assertEqual(l, ['-Lbardir', '-Lfoodir', '-lfoo', '-lbar'])

        ## Test that 'direct' append and extend works
        l = cc.compiler_args(['-Lfoodir', '-lfoo'])
        self.assertEqual(l, ['-Lfoodir', '-lfoo'])
        # Direct-adding a library and a libpath appends both correctly
        l.extend_direct(['-Lbardir', '-lbar'])
        self.assertEqual(l, ['-Lfoodir', '-lfoo', '-Lbardir', '-lbar'])
        # Direct-adding the same library again still adds it
        l.append_direct('-lbar')
        self.assertEqual(l, ['-Lfoodir', '-lfoo', '-Lbardir', '-lbar', '-lbar'])
        # Direct-adding with absolute path deduplicates
        abspath = str(Path('/libbaz.a').resolve())
        l.append_direct(abspath)
        self.assertEqual(l, ['-Lfoodir', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath])
        # Adding libbaz again does nothing
        l.append_direct(abspath)
        self.assertEqual(l, ['-Lfoodir', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath])


    def test_compiler_args_class_visualstudio(self):
        env = get_fake_env()
        linker = linkers.MSVCDynamicLinker(env, MachineChoice.HOST, [])
        # Version just needs to be > 19.0.0
        cc = VisualStudioCPPCompiler([], [], '20.00', MachineChoice.HOST, env, 'x64', linker=linker)

        a = cc.compiler_args(cc.get_always_args())
        self.assertEqual(a.to_native(copy=True), ['/nologo', '/showIncludes', '/utf-8', '/Zc:__cplusplus'])

        # Ensure /source-charset: removes /utf-8
        a.append('/source-charset:utf-8')
        self.assertEqual(a.to_native(copy=True), ['/nologo', '/showIncludes', '/Zc:__cplusplus', '/source-charset:utf-8'])

        # Ensure /execution-charset: removes /utf-8
        a = cc.compiler_args(cc.get_always_args() + ['/execution-charset:utf-8'])
        self.assertEqual(a.to_native(copy=True), ['/nologo', '/showIncludes', '/Zc:__cplusplus', '/execution-charset:utf-8'])

        # Ensure /validate-charset- removes /utf-8
        a = cc.compiler_args(cc.get_always_args() + ['/validate-charset-'])
        self.assertEqual(a.to_native(copy=True), ['/nologo', '/showIncludes', '/Zc:__cplusplus', '/validate-charset-'])


    def test_msvc_unix_args_to_native(self):
        # joined
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-isystemfoo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-idirafterfoo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-iquotefoo']), ['/Ifoo'])

        # with = separator
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-isystem=foo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-idirafter=foo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-iquote=foo']), ['/Ifoo'])

        # as separate argument
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-isystem', 'foo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-idirafter', 'foo']), ['/Ifoo'])
        self.assertEqual(MSVCCompiler.unix_args_to_native(['-iquote', 'foo']), ['/Ifoo'])

    def test_clangcl_unix_args_to_native(self):
        # joined
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-isystemfoo']), ['/clang:-isystemfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-idirafterfoo']), ['/clang:-idirafterfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-iquotefoo']), ['/clang:-iquotefoo'])

        # with = separator
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-isystem=foo']), ['/clang:-isystemfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-idirafter=foo']), ['/clang:-idirafterfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-iquote=foo']), ['/clang:-iquotefoo'])

        # as separate argument
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-isystem', 'foo']), ['/clang:-isystemfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-idirafter', 'foo']), ['/clang:-idirafterfoo'])
        self.assertEqual(ClangClCompiler.unix_args_to_native(['-iquote', 'foo']), ['/clang:-iquotefoo'])

    def test_compiler_args_class_gnuld(self):
        ## Test --start/end-group
        env = get_fake_env()
        linker = linkers.GnuBFDDynamicLinker([], env, MachineChoice.HOST, ManyInOneLinkerOptionStyle('-Wl,', ','), [])
        gcc = GnuCCompiler([], [], 'fake', MachineChoice.HOST, env, linker=linker)
        ## Ensure that the fake compiler is never called by overriding the relevant function
        gcc.get_default_include_dirs = lambda: ['/usr/include', '/usr/share/include', '/usr/local/include']
        ## Test that 'direct' append and extend works
        l = gcc.compiler_args(['-Lfoodir', '-lfoo'])
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-lfoo'])
        # Direct-adding a library and a libpath appends both correctly
        l.extend_direct(['-Lbardir', '-lbar'])
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-Wl,--end-group'])
        # Direct-adding the same library again still adds it
        l.append_direct('-lbar')
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-lbar', '-Wl,--end-group'])
        # Direct-adding with absolute path deduplicates
        abspath = str(Path('/libbaz.a').resolve())
        l.append_direct(abspath)
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath, '-Wl,--end-group'])
        # Adding libbaz again does nothing
        l.append_direct(abspath)
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath, '-Wl,--end-group'])
        # Adding a non-library argument doesn't include it in the group
        l += ['-Lfoo', '-Wl,--export-dynamic']
        self.assertEqual(l.to_native(copy=True), ['-Lfoo', '-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath, '-Wl,--end-group', '-Wl,--export-dynamic'])
        # -Wl,-lfoo is detected as a library and gets added to the group
        l.append('-Wl,-ldl')
        self.assertEqual(l.to_native(copy=True), ['-Lfoo', '-Lfoodir', '-Wl,--start-group', '-lfoo', '-Lbardir', '-lbar', '-lbar', abspath, '-Wl,--export-dynamic', '-Wl,-ldl', '-Wl,--end-group'])

    def test_compiler_args_remove_system(self):
        ## Test --start/end-group
        env = get_fake_env()
        linker = linkers.GnuBFDDynamicLinker([], env, MachineChoice.HOST, ManyInOneLinkerOptionStyle('-Wl,', ','), [])
        gcc = GnuCCompiler([], [], 'fake', MachineChoice.HOST, env, linker=linker)
        ## Ensure that the fake compiler is never called by overriding the relevant function
        gcc.get_default_include_dirs = lambda: ['/usr/include', '/usr/share/include', '/usr/local/include']
        ## Test that 'direct' append and extend works
        l = gcc.compiler_args(['-Lfoodir', '-lfoo'])
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-lfoo'])
        ## Test that to_native removes all system includes
        l += ['-isystem/usr/include', '-isystem=/usr/share/include', '-DSOMETHING_IMPORTANT=1', '-isystem', '/usr/local/include']
        self.assertEqual(l.to_native(copy=True), ['-Lfoodir', '-lfoo', '-DSOMETHING_IMPORTANT=1'])

    def test_string_templates_substitution(self):
        dictfunc = mesonbuild.mesonlib.get_filenames_templates_dict
        substfunc = mesonbuild.mesonlib.substitute_values
        ME = mesonbuild.mesonlib.MesonException

        # Identity
        self.assertEqual(dictfunc([], []), {})

        # One input, no outputs
        inputs = ['bar/foo.c.in']
        outputs = []
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0],
             '@PLAINNAME0@': 'foo.c.in', '@BASENAME0@': 'foo.c',
             '@PLAINNAME@': 'foo.c.in', '@BASENAME@': 'foo.c'}
        # Check dictionary
        self.assertEqual(ret, d)
        # Check substitutions
        cmd = ['some', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), cmd)
        cmd = ['@INPUT@.out', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), [inputs[0] + '.out'] + cmd[1:])
        cmd = ['@INPUT0@.out', '@PLAINNAME@.ok', 'strings']
        self.assertEqual(substfunc(cmd, d),
                         [inputs[0] + '.out'] + [d['@PLAINNAME@'] + '.ok'] + cmd[2:])
        cmd = ['@INPUT@', '@BASENAME@.hah', 'strings']
        self.assertEqual(substfunc(cmd, d),
                         inputs + [d['@BASENAME@'] + '.hah'] + cmd[2:])
        cmd = ['@OUTPUT@']
        self.assertRaises(ME, substfunc, cmd, d)

        # One input, one output
        inputs = ['bar/foo.c.in']
        outputs = ['out.c']
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0],
             '@PLAINNAME0@': 'foo.c.in', '@BASENAME0@': 'foo.c',
             '@PLAINNAME@': 'foo.c.in', '@BASENAME@': 'foo.c',
             '@OUTPUT@': outputs, '@OUTPUT0@': outputs[0], '@OUTDIR@': '.'}
        # Check dictionary
        self.assertEqual(ret, d)
        # Check substitutions
        cmd = ['some', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), cmd)
        cmd = ['@INPUT@ @OUTPUT@']
        self.assertEqual(substfunc(cmd, d),
                         [f'{inputs[0]} {outputs[0]}'])
        cmd = ['@INPUT@.out', '@OUTPUT@', 'strings']
        self.assertEqual(substfunc(cmd, d),
                         [inputs[0] + '.out'] + outputs + cmd[2:])
        cmd = ['@INPUT0@.out', '@PLAINNAME@.ok', '@OUTPUT0@']
        self.assertEqual(substfunc(cmd, d),
                         [inputs[0] + '.out', d['@PLAINNAME@'] + '.ok'] + outputs)
        cmd = ['@INPUT@', '@BASENAME@.hah', 'strings']
        self.assertEqual(substfunc(cmd, d),
                         inputs + [d['@BASENAME@'] + '.hah'] + cmd[2:])

        # One input, one output with a subdir
        outputs = ['dir/out.c']
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0],
             '@PLAINNAME0@': 'foo.c.in', '@BASENAME0@': 'foo.c',
             '@PLAINNAME@': 'foo.c.in', '@BASENAME@': 'foo.c',
             '@OUTPUT@': outputs, '@OUTPUT0@': outputs[0], '@OUTDIR@': 'dir'}
        # Check dictionary
        self.assertEqual(ret, d)

        # Two inputs, no outputs
        inputs = ['bar/foo.c.in', 'baz/foo.c.in']
        outputs = []
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0], '@INPUT1@': inputs[1],
             '@PLAINNAME0@': 'foo.c.in', '@PLAINNAME1@': 'foo.c.in',
             '@BASENAME0@': 'foo.c', '@BASENAME1@': 'foo.c'}
        # Check dictionary
        self.assertEqual(ret, d)
        # Check substitutions
        cmd = ['some', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), cmd)
        cmd = ['@INPUT@', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), inputs + cmd[1:])
        cmd = ['@INPUT0@.out', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), [inputs[0] + '.out'] + cmd[1:])
        cmd = ['@INPUT0@.out', '@INPUT1@.ok', 'strings']
        self.assertEqual(substfunc(cmd, d), [inputs[0] + '.out', inputs[1] + '.ok'] + cmd[2:])
        cmd = ['@INPUT0@', '@INPUT1@', 'strings']
        self.assertEqual(substfunc(cmd, d), inputs + cmd[2:])
        # Many inputs, can't use @INPUT@ like this
        cmd = ['@INPUT@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Not enough inputs
        cmd = ['@INPUT2@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Too many inputs
        cmd = ['@PLAINNAME@']
        self.assertRaises(ME, substfunc, cmd, d)
        cmd = ['@BASENAME@']
        self.assertRaises(ME, substfunc, cmd, d)
        # No outputs
        cmd = ['@OUTPUT@']
        self.assertRaises(ME, substfunc, cmd, d)
        cmd = ['@OUTPUT0@']
        self.assertRaises(ME, substfunc, cmd, d)
        cmd = ['@OUTDIR@']
        self.assertRaises(ME, substfunc, cmd, d)

        # Two inputs, one output
        outputs = ['dir/out.c']
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0], '@INPUT1@': inputs[1],
             '@PLAINNAME0@': 'foo.c.in', '@PLAINNAME1@': 'foo.c.in',
             '@BASENAME0@': 'foo.c', '@BASENAME1@': 'foo.c',
             '@OUTPUT@': outputs, '@OUTPUT0@': outputs[0], '@OUTDIR@': 'dir'}
        # Check dictionary
        self.assertEqual(ret, d)
        # Check substitutions
        cmd = ['some', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), cmd)
        cmd = ['@OUTPUT@', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), outputs + cmd[1:])
        cmd = ['@OUTPUT@.out', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), [outputs[0] + '.out'] + cmd[1:])
        cmd = ['@OUTPUT0@.out', '@INPUT1@.ok', 'strings']
        self.assertEqual(substfunc(cmd, d), [outputs[0] + '.out', inputs[1] + '.ok'] + cmd[2:])
        # Many inputs, can't use @INPUT@ like this
        cmd = ['@INPUT@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Not enough inputs
        cmd = ['@INPUT2@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Not enough outputs
        cmd = ['@OUTPUT2@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)

        # Two inputs, two outputs
        outputs = ['dir/out.c', 'dir/out2.c']
        ret = dictfunc(inputs, outputs)
        d = {'@INPUT@': inputs, '@INPUT0@': inputs[0], '@INPUT1@': inputs[1],
             '@PLAINNAME0@': 'foo.c.in', '@PLAINNAME1@': 'foo.c.in',
             '@BASENAME0@': 'foo.c', '@BASENAME1@': 'foo.c',
             '@OUTPUT@': outputs, '@OUTPUT0@': outputs[0], '@OUTPUT1@': outputs[1],
             '@OUTDIR@': 'dir'}
        # Check dictionary
        self.assertEqual(ret, d)
        # Check substitutions
        cmd = ['some', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), cmd)
        cmd = ['@OUTPUT@', 'ordinary', 'strings']
        self.assertEqual(substfunc(cmd, d), outputs + cmd[1:])
        cmd = ['@OUTPUT0@', '@OUTPUT1@', 'strings']
        self.assertEqual(substfunc(cmd, d), outputs + cmd[2:])
        cmd = ['@OUTPUT0@.out', '@INPUT1@.ok', '@OUTDIR@']
        self.assertEqual(substfunc(cmd, d), [outputs[0] + '.out', inputs[1] + '.ok', 'dir'])
        # Many inputs, can't use @INPUT@ like this
        cmd = ['@INPUT@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Not enough inputs
        cmd = ['@INPUT2@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Not enough outputs
        cmd = ['@OUTPUT2@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)
        # Many outputs, can't use @OUTPUT@ like this
        cmd = ['@OUTPUT@.out', 'ordinary', 'strings']
        self.assertRaises(ME, substfunc, cmd, d)

    def test_needs_exe_wrapper_override(self):
        config = ConfigParser()
        config['binaries'] = {
            'c': '\'/usr/bin/gcc\'',
        }
        config['host_machine'] = {
            'system': '\'linux\'',
            'cpu_family': '\'arm\'',
            'cpu': '\'armv7\'',
            'endian': '\'little\'',
        }
        # Can not be used as context manager because we need to
        # open it a second time and this is not possible on
        # Windows.
        configfile = tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8')
        configfilename = configfile.name
        config.write(configfile)
        configfile.flush()
        configfile.close()
        opts = get_fake_options()
        opts.cross_file = (configfilename,)
        env = get_fake_env(opts=opts)
        detected_value = env.need_exe_wrapper()
        os.unlink(configfilename)

        desired_value = not detected_value
        config['properties'] = {
            'needs_exe_wrapper': 'true' if desired_value else 'false'
        }

        configfile = tempfile.NamedTemporaryFile(mode='w+', delete=False, encoding='utf-8')
        configfilename = configfile.name
        config.write(configfile)
        configfile.close()
        opts = get_fake_options()
        opts.cross_file = (configfilename,)
        env = get_fake_env(opts=opts)
        forced_value = env.need_exe_wrapper()
        os.unlink(configfilename)

        self.assertEqual(forced_value, desired_value)

    def test_listify(self):
        listify = mesonbuild.mesonlib.listify
        # Test sanity
        self.assertEqual([1], listify(1))
        self.assertEqual([], listify([]))
        self.assertEqual([1], listify([1]))
        # Test flattening
        self.assertEqual([1, 2, 3], listify([1, [2, 3]]))
        self.assertEqual([1, 2, 3], listify([1, [2, [3]]]))
        self.assertEqual([1, [2, [3]]], listify([1, [2, [3]]], flatten=False))
        # Test flattening and unholdering
        class TestHeldObj(mesonbuild.mesonlib.HoldableObject):
            def __init__(self, val: int) -> None:
                self._val = val
        class MockInterpreter:
            def __init__(self) -> None:
                self.subproject = ''
                self.environment = None
        heldObj1 = TestHeldObj(1)
        holder1 = ObjectHolder(heldObj1, MockInterpreter())
        self.assertEqual([holder1], listify(holder1))
        self.assertEqual([holder1], listify([holder1]))
        self.assertEqual([holder1, 2], listify([holder1, 2]))
        self.assertEqual([holder1, 2, 3], listify([holder1, 2, [3]]))

    def test_extract_as_list(self):
        extract = mesonbuild.mesonlib.extract_as_list
        # Test sanity
        kwargs = {'sources': [1, 2, 3]}
        self.assertEqual([1, 2, 3], extract(kwargs, 'sources'))
        self.assertEqual(kwargs, {'sources': [1, 2, 3]})
        self.assertEqual([1, 2, 3], extract(kwargs, 'sources', pop=True))
        self.assertEqual(kwargs, {})

        class TestHeldObj(mesonbuild.mesonlib.HoldableObject):
            pass
        class MockInterpreter:
            def __init__(self) -> None:
                self.subproject = ''
                self.environment = None
        heldObj = TestHeldObj()

        # Test unholding
        holder3 = ObjectHolder(heldObj, MockInterpreter())
        kwargs = {'sources': [1, 2, holder3]}
        self.assertEqual(kwargs, {'sources': [1, 2, holder3]})

        # flatten nested lists
        kwargs = {'sources': [1, [2, [3]]]}
        self.assertEqual([1, 2, 3], extract(kwargs, 'sources'))

    def _test_all_naming(self, cc, patterns, platform):
        shr = patterns[platform]['shared']
        stc = patterns[platform]['static']
        shrstc = shr + tuple(x for x in stc if x not in shr)
        stcshr = stc + tuple(x for x in shr if x not in stc)
        p = cc.get_library_naming(LibType.SHARED)
        self.assertEqual(p, shr)
        p = cc.get_library_naming(LibType.STATIC)
        self.assertEqual(p, stc)
        p = cc.get_library_naming(LibType.PREFER_STATIC)
        self.assertEqual(p, stcshr)
        p = cc.get_library_naming(LibType.PREFER_SHARED)
        self.assertEqual(p, shrstc)
        # Test find library by mocking up openbsd
        if platform != 'openbsd':
            return
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in ['libfoo.so.6.0', 'libfoo.so.5.0', 'libfoo.so.54.0', 'libfoo.so.66a.0b', 'libfoo.so.70.0.so.1',
                      'libbar.so.7.10', 'libbar.so.7.9', 'libbar.so.7.9.3']:
                libpath = Path(tmpdir) / i
                src = libpath.with_suffix('.c')
                with src.open('w', encoding='utf-8') as f:
                    f.write('int meson_foobar (void) { return 0; }')
                subprocess.check_call(['gcc', str(src), '-o', str(libpath), '-shared'])

            found = cc._find_library_real('foo', [tmpdir], 'int main(void) { return 0; }', LibType.PREFER_SHARED, lib_prefix_warning=True, ignore_system_dirs=False)
            self.assertEqual(os.path.basename(found[0]), 'libfoo.so.54.0')
            found = cc._find_library_real('bar', [tmpdir], 'int main(void) { return 0; }', LibType.PREFER_SHARED, lib_prefix_warning=True, ignore_system_dirs=False)
            self.assertEqual(os.path.basename(found[0]), 'libbar.so.7.10')

    def test_find_library_patterns(self):
        '''
        Unit test for the library search patterns used by find_library()
        '''
        unix_static = ('lib{}.a', '{}.a')
        msvc_static = ('lib{}.a', 'lib{}.lib', '{}.a', '{}.lib')
        # This is the priority list of pattern matching for library searching
        patterns = {'openbsd': {'shared': ('lib{}.so', '{}.so', 'lib{}.so.[0-9]*.[0-9]*', '{}.so.[0-9]*.[0-9]*'),
                                'static': unix_static},
                    'linux': {'shared': ('lib{}.so', '{}.so'),
                              'static': unix_static},
                    'darwin': {'shared': ('lib{}.dylib', 'lib{}.so', '{}.dylib', '{}.so'),
                               'static': unix_static},
                    'cygwin': {'shared': ('cyg{}.dll', 'cyg{}.dll.a', 'lib{}.dll',
                                          'lib{}.dll.a', '{}.dll', '{}.dll.a'),
                               'static': ('cyg{}.a',) + unix_static},
                    'windows-msvc': {'shared': ('lib{}.lib', '{}.lib'),
                                     'static': msvc_static},
                    'windows-mingw': {'shared': ('lib{}.dll.a', 'lib{}.lib', 'lib{}.dll',
                                                 '{}.dll.a', '{}.lib', '{}.dll'),
                                      'static': msvc_static}}
        env = get_fake_env()
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if is_osx():
            self._test_all_naming(cc, patterns, 'darwin')
        elif is_cygwin():
            self._test_all_naming(cc, patterns, 'cygwin')
        elif is_windows():
            if cc.get_argument_syntax() == 'msvc':
                self._test_all_naming(cc, patterns, 'windows-msvc')
            else:
                self._test_all_naming(cc, patterns, 'windows-mingw')
        elif is_openbsd():
            self._test_all_naming(cc, patterns, 'openbsd')
        else:
            self._test_all_naming(cc, patterns, 'linux')
            env.machines.host.system = 'openbsd'
            self._test_all_naming(cc, patterns, 'openbsd')
            env.machines.host.system = 'darwin'
            self._test_all_naming(cc, patterns, 'darwin')
            env.machines.host.system = 'cygwin'
            self._test_all_naming(cc, patterns, 'cygwin')
            env.machines.host.system = 'windows'
            self._test_all_naming(cc, patterns, 'windows-mingw')

    @skipIfNoPkgconfig
    def test_pkgconfig_parse_libs(self):
        '''
        Unit test for parsing of pkg-config output to search for libraries

        https://github.com/mesonbuild/meson/issues/3951
        '''
        def create_static_lib(name):
            src = name.with_suffix('.c')
            out = name.with_suffix('.o')
            with src.open('w', encoding='utf-8') as f:
                f.write('int meson_foobar (void) { return 0; }')
            # use of x86_64 is hardcoded in run_tests.py:get_fake_env()
            if is_osx():
                subprocess.check_call(['clang', '-c', str(src), '-o', str(out), '-arch', 'x86_64'])
            else:
                subprocess.check_call(['gcc', '-c', str(src), '-o', str(out)])
            subprocess.check_call(['ar', 'csr', str(name), str(out)])

        # The test relies on some open-coded toolchain invocations for
        # library creation in create_static_lib.
        if is_windows() or is_cygwin():
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            pkgbin = ExternalProgram('pkg-config', command=['pkg-config'], silent=True)
            env = get_fake_env()
            compiler = detect_c_compiler(env, MachineChoice.HOST)
            env.coredata.compilers.host = {'c': compiler}
            p1 = Path(tmpdir) / '1'
            p2 = Path(tmpdir) / '2'
            p1.mkdir()
            p2.mkdir()
            # libfoo.a is in one prefix
            create_static_lib(p1 / 'libfoo.a')
            # libbar.a is in both prefixes
            create_static_lib(p1 / 'libbar.a')
            create_static_lib(p2 / 'libbar.a')
            # Ensure that we never statically link to these
            create_static_lib(p1 / 'libpthread.a')
            create_static_lib(p1 / 'libm.a')
            create_static_lib(p1 / 'libc.a')
            create_static_lib(p1 / 'libdl.a')
            create_static_lib(p1 / 'librt.a')

            class FakeInstance(PkgConfigCLI):
                def _call_pkgbin(self, args, env=None):
                    if '--libs' not in args:
                        return 0, '', ''
                    if args[-1] == 'foo':
                        return 0, f'-L{p2.as_posix()} -lfoo -L{p1.as_posix()} -lbar', ''
                    if args[-1] == 'bar':
                        return 0, f'-L{p2.as_posix()} -lbar', ''
                    if args[-1] == 'internal':
                        return 0, f'-L{p1.as_posix()} -lpthread -lm -lc -lrt -ldl', ''

            with mock.patch.object(PkgConfigInterface, 'instance') as instance_method:
                instance_method.return_value = FakeInstance(env, MachineChoice.HOST, silent=True)
                kwargs = {'required': True, 'silent': True, 'native': MachineChoice.HOST}
                foo_dep = PkgConfigDependency('foo', env, kwargs)
                self.assertEqual(foo_dep.get_link_args(),
                                 [(p1 / 'libfoo.a').as_posix(), (p2 / 'libbar.a').as_posix()])
                bar_dep = PkgConfigDependency('bar', env, kwargs)
                self.assertEqual(bar_dep.get_link_args(), [(p2 / 'libbar.a').as_posix()])
                internal_dep = PkgConfigDependency('internal', env, kwargs)
                if compiler.get_argument_syntax() == 'msvc':
                    self.assertEqual(internal_dep.get_link_args(), [])
                else:
                    link_args = internal_dep.get_link_args()
                    for link_arg in link_args:
                        for lib in ('pthread', 'm', 'c', 'dl', 'rt'):
                            self.assertNotIn(f'lib{lib}.a', link_arg, msg=link_args)

    def test_program_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / 'script.py'
            script_path.write_text('import sys\nprint(sys.argv[1])\n', encoding='utf-8')
            script_path.chmod(0o755)

            inputs: list[tuple[str, str | None]] = [
                ('',  None),
                ('1',  None),
                ('1.2.4',  '1.2.4'),
                ('1 1.2.4',  '1.2.4'),
                ('foo version 1.2.4',  '1.2.4'),
                ('foo 1.2.4.',  '1.2.4'),
                ('foo 1.2.4',  '1.2.4'),
                ('foo 1.2.4 bar',  '1.2.4'),
                ('foo 10.0.0',  '10.0.0'),
                ('50 5.4.0',  '5.4.0'),
                ('This is perl 5, version 40, subversion 0 (v5.40.0)',  '5.40.0'),
                ('git version 2.48.0.rc1',  '2.48.0'),
            ]

            for output, expected in inputs:
                prog = ExternalProgram('script', command=python_command + [str(script_path), output], silent=True)

                with self.subTest(output=output, expected=expected):
                    if expected is None:
                        with self.assertRaisesRegex(MesonException, 'Could not find a version number'):
                            prog.get_version()
                    else:
                        self.assertEqual(prog.get_version(), expected)

    def test_version_compare(self):
        comparefunc = mesonbuild.mesonlib.version_compare_many
        for (a, b, result) in [
                ('0.99.beta19', '>= 0.99.beta14', True),
        ]:
            self.assertEqual(comparefunc(a, b)[0], result)

        for (a, b, op) in [
                # examples from https://fedoraproject.org/wiki/Archive:Tools/RPM/VersionComparison
                ("1.0010", "1.9", operator.gt),
                ("1.05", "1.5", operator.eq),
                ("1.0", "1", operator.gt),
                ("2.50", "2.5", operator.gt),
                ("fc4", "fc.4", operator.eq),
                ("FC5", "fc4", operator.lt),
                ("2a", "2.0", operator.lt),
                ("1.0", "1.fc4", operator.gt),
                ("3.0.0_fc", "3.0.0.fc", operator.eq),
                # from RPM tests
                ("1.0", "1.0", operator.eq),
                ("1.0", "2.0", operator.lt),
                ("2.0", "1.0", operator.gt),
                ("2.0.1", "2.0.1", operator.eq),
                ("2.0", "2.0.1", operator.lt),
                ("2.0.1", "2.0", operator.gt),
                ("2.0.1a", "2.0.1a", operator.eq),
                ("2.0.1a", "2.0.1", operator.gt),
                ("2.0.1", "2.0.1a", operator.lt),
                ("5.5p1", "5.5p1", operator.eq),
                ("5.5p1", "5.5p2", operator.lt),
                ("5.5p2", "5.5p1", operator.gt),
                ("5.5p10", "5.5p10", operator.eq),
                ("5.5p1", "5.5p10", operator.lt),
                ("5.5p10", "5.5p1", operator.gt),
                ("10xyz", "10.1xyz", operator.lt),
                ("10.1xyz", "10xyz", operator.gt),
                ("xyz10", "xyz10", operator.eq),
                ("xyz10", "xyz10.1", operator.lt),
                ("xyz10.1", "xyz10", operator.gt),
                ("xyz.4", "xyz.4", operator.eq),
                ("xyz.4", "8", operator.lt),
                ("8", "xyz.4", operator.gt),
                ("xyz.4", "2", operator.lt),
                ("2", "xyz.4", operator.gt),
                ("5.5p2", "5.6p1", operator.lt),
                ("5.6p1", "5.5p2", operator.gt),
                ("5.6p1", "6.5p1", operator.lt),
                ("6.5p1", "5.6p1", operator.gt),
                ("6.0.rc1", "6.0", operator.gt),
                ("6.0", "6.0.rc1", operator.lt),
                ("10b2", "10a1", operator.gt),
                ("10a2", "10b2", operator.lt),
                ("1.0aa", "1.0aa", operator.eq),
                ("1.0a", "1.0aa", operator.lt),
                ("1.0aa", "1.0a", operator.gt),
                ("10.0001", "10.0001", operator.eq),
                ("10.0001", "10.1", operator.eq),
                ("10.1", "10.0001", operator.eq),
                ("10.0001", "10.0039", operator.lt),
                ("10.0039", "10.0001", operator.gt),
                ("4.999.9", "5.0", operator.lt),
                ("5.0", "4.999.9", operator.gt),
                ("20101121", "20101121", operator.eq),
                ("20101121", "20101122", operator.lt),
                ("20101122", "20101121", operator.gt),
                ("2_0", "2_0", operator.eq),
                ("2.0", "2_0", operator.eq),
                ("2_0", "2.0", operator.eq),
                ("a", "a", operator.eq),
                ("a+", "a+", operator.eq),
                ("a+", "a_", operator.eq),
                ("a_", "a+", operator.eq),
                ("+a", "+a", operator.eq),
                ("+a", "_a", operator.eq),
                ("_a", "+a", operator.eq),
                ("+_", "+_", operator.eq),
                ("_+", "+_", operator.eq),
                ("_+", "_+", operator.eq),
                ("+", "_", operator.eq),
                ("_", "+", operator.eq),
                # other tests
                ('0.99.beta19', '0.99.beta14', operator.gt),
                ("1.0.0", "2.0.0", operator.lt),
                (".0.0", "2.0.0", operator.lt),
                ("alpha", "beta", operator.lt),
                ("1.0", "1.0.0", operator.lt),
                ("2.456", "2.1000", operator.lt),
                ("2.1000", "3.111", operator.lt),
                ("2.001", "2.1", operator.eq),
                ("2.34", "2.34", operator.eq),
                ("6.1.2", "6.3.8", operator.lt),
                ("1.7.3.0", "2.0.0", operator.lt),
                ("2.24.51", "2.25", operator.lt),
                ("2.1.5+20120813+gitdcbe778", "2.1.5", operator.gt),
                ("3.4.1", "3.4b1", operator.gt),
                ("041206", "200090325", operator.lt),
                ("0.6.2+git20130413", "0.6.2", operator.gt),
                ("2.6.0+bzr6602", "2.6.0", operator.gt),
                ("2.6.0", "2.6b2", operator.gt),
                ("2.6.0+bzr6602", "2.6b2x", operator.gt),
                ("0.6.7+20150214+git3a710f9", "0.6.7", operator.gt),
                ("15.8b", "15.8.0.1", operator.lt),
                ("1.2rc1", "1.2.0", operator.lt),
        ]:
            ver_a = Version(a)
            ver_b = Version(b)
            if op is operator.eq:
                for o, name in [(op, 'eq'), (operator.ge, 'ge'), (operator.le, 'le')]:
                    self.assertTrue(o(ver_a, ver_b), f'{ver_a} {name} {ver_b}')
            if op is operator.lt:
                for o, name in [(op, 'lt'), (operator.le, 'le'), (operator.ne, 'ne')]:
                    self.assertTrue(o(ver_a, ver_b), f'{ver_a} {name} {ver_b}')
                for o, name in [(operator.gt, 'gt'), (operator.ge, 'ge'), (operator.eq, 'eq')]:
                    self.assertFalse(o(ver_a, ver_b), f'{ver_a} {name} {ver_b}')
            if op is operator.gt:
                for o, name in [(op, 'gt'), (operator.ge, 'ge'), (operator.ne, 'ne')]:
                    self.assertTrue(o(ver_a, ver_b), f'{ver_a} {name} {ver_b}')
                for o, name in [(operator.lt, 'lt'), (operator.le, 'le'), (operator.eq, 'eq')]:
                    self.assertFalse(o(ver_a, ver_b), f'{ver_a} {name} {ver_b}')

    def test_split_args(self):
        split_args = mesonbuild.mesonlib.split_args
        join_args = mesonbuild.mesonlib.join_args
        if is_windows():
            test_data = [
                # examples from https://docs.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments
                (r'"a b c" d e', ['a b c', 'd', 'e'], True),
                (r'"ab\"c" "\\" d', ['ab"c', '\\', 'd'], False),
                (r'a\\\b d"e f"g h', [r'a\\\b', 'de fg', 'h'], False),
                (r'a\\\"b c d', [r'a\"b', 'c', 'd'], False),
                (r'a\\\\"b c" d e', [r'a\\b c', 'd', 'e'], False),
                # other basics
                (r'""', [''], True),
                (r'a b c d "" e', ['a', 'b', 'c', 'd', '', 'e'], True),
                (r"'a b c' d e", ["'a", 'b', "c'", 'd', 'e'], True),
                (r"'a&b&c' d e", ["'a&b&c'", 'd', 'e'], True),
                (r"a & b & c d e", ['a', '&', 'b', '&', 'c', 'd', 'e'], True),
                (r"'a & b & c d e'", ["'a", '&', 'b', '&', 'c', 'd', "e'"], True),
                ('a  b\nc\rd \n\re', ['a', 'b', 'c', 'd', 'e'], False),
                # more illustrative tests
                (r'cl test.cpp /O1 /Fe:test.exe', ['cl', 'test.cpp', '/O1', '/Fe:test.exe'], True),
                (r'cl "test.cpp /O1 /Fe:test.exe"', ['cl', 'test.cpp /O1 /Fe:test.exe'], True),
                (r'cl /DNAME=\"Bob\" test.cpp', ['cl', '/DNAME="Bob"', 'test.cpp'], False),
                (r'cl "/DNAME=\"Bob\"" test.cpp', ['cl', '/DNAME="Bob"', 'test.cpp'], True),
                (r'cl /DNAME=\"Bob, Alice\" test.cpp', ['cl', '/DNAME="Bob,', 'Alice"', 'test.cpp'], False),
                (r'cl "/DNAME=\"Bob, Alice\"" test.cpp', ['cl', '/DNAME="Bob, Alice"', 'test.cpp'], True),
                (r'cl C:\path\with\backslashes.cpp', ['cl', r'C:\path\with\backslashes.cpp'], True),
                (r'cl C:\\path\\with\\double\\backslashes.cpp', ['cl', r'C:\\path\\with\\double\\backslashes.cpp'], True),
                (r'cl "C:\\path\\with\\double\\backslashes.cpp"', ['cl', r'C:\\path\\with\\double\\backslashes.cpp'], False),
                (r'cl C:\path with spaces\test.cpp', ['cl', r'C:\path', 'with', r'spaces\test.cpp'], False),
                (r'cl "C:\path with spaces\test.cpp"', ['cl', r'C:\path with spaces\test.cpp'], True),
                (r'cl /DPATH="C:\path\with\backslashes test.cpp', ['cl', r'/DPATH=C:\path\with\backslashes test.cpp'], False),
                (r'cl /DPATH=\"C:\\ends\\with\\backslashes\\\" test.cpp', ['cl', r'/DPATH="C:\\ends\\with\\backslashes\"', 'test.cpp'], False),
                (r'cl /DPATH="C:\\ends\\with\\backslashes\\" test.cpp', ['cl', '/DPATH=C:\\\\ends\\\\with\\\\backslashes\\', 'test.cpp'], False),
                (r'cl "/DNAME=\"C:\\ends\\with\\backslashes\\\"" test.cpp', ['cl', r'/DNAME="C:\\ends\\with\\backslashes\"', 'test.cpp'], True),
                (r'cl "/DNAME=\"C:\\ends\\with\\backslashes\\\\"" test.cpp', ['cl', r'/DNAME="C:\\ends\\with\\backslashes\\ test.cpp'], False),
                (r'cl "/DNAME=\"C:\\ends\\with\\backslashes\\\\\"" test.cpp', ['cl', r'/DNAME="C:\\ends\\with\\backslashes\\"', 'test.cpp'], True),
            ]
        else:
            test_data = [
                (r"'a b c' d e", ['a b c', 'd', 'e'], True),
                (r"a/b/c d e", ['a/b/c', 'd', 'e'], True),
                (r"a\b\c d e", [r'abc', 'd', 'e'], False),
                (r"a\\b\\c d e", [r'a\b\c', 'd', 'e'], False),
                (r'"a b c" d e', ['a b c', 'd', 'e'], False),
                (r'"a\\b\\c\\" d e', ['a\\b\\c\\', 'd', 'e'], False),
                (r"'a\b\c\' d e", ['a\\b\\c\\', 'd', 'e'], True),
                (r"'a&b&c' d e", ['a&b&c', 'd', 'e'], True),
                (r"a & b & c d e", ['a', '&', 'b', '&', 'c', 'd', 'e'], False),
                (r"'a & b & c d e'", ['a & b & c d e'], True),
                (r"abd'e f'g h", [r'abde fg', 'h'], False),
                ('a  b\nc\rd \n\re', ['a', 'b', 'c', 'd', 'e'], False),

                ('g++ -DNAME="Bob" test.cpp', ['g++', '-DNAME=Bob', 'test.cpp'], False),
                ("g++ '-DNAME=\"Bob\"' test.cpp", ['g++', '-DNAME="Bob"', 'test.cpp'], True),
                ('g++ -DNAME="Bob, Alice" test.cpp', ['g++', '-DNAME=Bob, Alice', 'test.cpp'], False),
                ("g++ '-DNAME=\"Bob, Alice\"' test.cpp", ['g++', '-DNAME="Bob, Alice"', 'test.cpp'], True),
            ]

        for (cmd, expected, roundtrip) in test_data:
            self.assertEqual(split_args(cmd), expected)
            if roundtrip:
                self.assertEqual(join_args(expected), cmd)

    def test_quote_arg(self):
        split_args = mesonbuild.mesonlib.split_args
        quote_arg = mesonbuild.mesonlib.quote_arg
        if is_windows():
            test_data = [
                ('', '""'),
                ('arg1', 'arg1'),
                ('/option1', '/option1'),
                ('/Ovalue', '/Ovalue'),
                ('/OBob&Alice', '/OBob&Alice'),
                ('/Ovalue with spaces', r'"/Ovalue with spaces"'),
                (r'/O"value with spaces"', r'"/O\"value with spaces\""'),
                (r'/OC:\path with spaces\test.exe', r'"/OC:\path with spaces\test.exe"'),
                ('/LIBPATH:C:\\path with spaces\\ends\\with\\backslashes\\', r'"/LIBPATH:C:\path with spaces\ends\with\backslashes\\"'),
                ('/LIBPATH:"C:\\path with spaces\\ends\\with\\backslashes\\\\"', r'"/LIBPATH:\"C:\path with spaces\ends\with\backslashes\\\\\""'),
                (r'/DMSG="Alice said: \"Let\'s go\""', r'"/DMSG=\"Alice said: \\\"Let\'s go\\\"\""'),
            ]
        else:
            test_data = [
                ('arg1', 'arg1'),
                ('--option1', '--option1'),
                ('-O=value', '-O=value'),
                ('-O=Bob&Alice', "'-O=Bob&Alice'"),
                ('-O=value with spaces', "'-O=value with spaces'"),
                ('-O="value with spaces"', '\'-O=\"value with spaces\"\''),
                ('-O=/path with spaces/test', '\'-O=/path with spaces/test\''),
                ('-DMSG="Alice said: \\"Let\'s go\\""', "'-DMSG=\"Alice said: \\\"Let'\"'\"'s go\\\"\"'"),
            ]

        for (arg, expected) in test_data:
            self.assertEqual(quote_arg(arg), expected)
            self.assertEqual(split_args(expected)[0], arg)

    def test_depfile(self):
        for (f, target, expdeps) in [
                # empty, unknown target
                ([''], 'unknown', set()),
                # simple target & deps
                (['meson/foo.o  : foo.c   foo.h'], 'meson/foo.o', set({'foo.c', 'foo.h'})),
                (['meson/foo.o: foo.c foo.h'], 'foo.c', set()),
                # get all deps
                (['meson/foo.o: foo.c foo.h',
                  'foo.c: gen.py'], 'meson/foo.o', set({'foo.c', 'foo.h', 'gen.py'})),
                (['meson/foo.o: foo.c foo.h',
                  'foo.c: gen.py'], 'foo.c', set({'gen.py'})),
                # linue continuation, multiple targets
                (['foo.o \\', 'foo.h: bar'], 'foo.h', set({'bar'})),
                (['foo.o \\', 'foo.h: bar'], 'foo.o', set({'bar'})),
                # \\ handling
                (['foo: Program\\ F\\iles\\\\X'], 'foo', set({'Program Files\\X'})),
                # $ handling
                (['f$o.o: c/b'], 'f$o.o', set({'c/b'})),
                (['f$$o.o: c/b'], 'f$o.o', set({'c/b'})),
                # cycles
                (['a: b', 'b: a'], 'a', set({'a', 'b'})),
                (['a: b', 'b: a'], 'b', set({'a', 'b'})),
        ]:
            d = mesonbuild.depfile.DepFile(f)
            deps = d.get_all_dependencies(target)
            self.assertEqual(sorted(deps), sorted(expdeps))

    def test_log_once(self):
        f = io.StringIO()
        with mock.patch('mesonbuild.mlog._logger.log_file', f), \
                mock.patch('mesonbuild.mlog._logger.logged_once', set()):
            mesonbuild.mlog.log('foo', once=True)
            mesonbuild.mlog.log('foo', once=True)
            actual = f.getvalue().strip()
            self.assertEqual(actual, 'foo', actual)

    def test_log_once_ansi(self):
        f = io.StringIO()
        with mock.patch('mesonbuild.mlog._logger.log_file', f), \
                mock.patch('mesonbuild.mlog._logger.logged_once', set()):
            mesonbuild.mlog.log(mesonbuild.mlog.bold('foo'), once=True)
            mesonbuild.mlog.log(mesonbuild.mlog.bold('foo'), once=True)
            actual = f.getvalue().strip()
            self.assertEqual(actual.count('foo'), 1, actual)

            mesonbuild.mlog.log('foo', once=True)
            actual = f.getvalue().strip()
            self.assertEqual(actual.count('foo'), 1, actual)

            f.truncate()

            mesonbuild.mlog.warning('bar', once=True)
            mesonbuild.mlog.warning('bar', once=True)
            actual = f.getvalue().strip()
            self.assertEqual(actual.count('bar'), 1, actual)

    def test_sort_libpaths(self):
        sort_libpaths = mesonbuild.dependencies.base.sort_libpaths
        self.assertEqual(sort_libpaths(
            ['/home/mesonuser/.local/lib', '/usr/local/lib', '/usr/lib'],
            ['/home/mesonuser/.local/lib/pkgconfig', '/usr/local/lib/pkgconfig']),
            ['/home/mesonuser/.local/lib', '/usr/local/lib', '/usr/lib'])
        self.assertEqual(sort_libpaths(
            ['/usr/local/lib', '/home/mesonuser/.local/lib', '/usr/lib'],
            ['/home/mesonuser/.local/lib/pkgconfig', '/usr/local/lib/pkgconfig']),
            ['/home/mesonuser/.local/lib', '/usr/local/lib', '/usr/lib'])
        self.assertEqual(sort_libpaths(
            ['/usr/lib', '/usr/local/lib', '/home/mesonuser/.local/lib'],
            ['/home/mesonuser/.local/lib/pkgconfig', '/usr/local/lib/pkgconfig']),
            ['/home/mesonuser/.local/lib', '/usr/local/lib', '/usr/lib'])
        self.assertEqual(sort_libpaths(
            ['/usr/lib', '/usr/local/lib', '/home/mesonuser/.local/lib'],
            ['/home/mesonuser/.local/lib/pkgconfig', '/usr/local/libdata/pkgconfig']),
            ['/home/mesonuser/.local/lib', '/usr/local/lib', '/usr/lib'])

    def test_dependency_factory_order(self):
        b = mesonbuild.dependencies.base
        F = mesonbuild.dependencies.factory
        with tempfile.TemporaryDirectory() as tmpdir:
            with chdir(tmpdir):
                env = get_fake_env()
                env.scratch_dir = tmpdir

                f = F.DependencyFactory(
                    'test_dep',
                    methods=[b.DependencyMethods.PKGCONFIG, b.DependencyMethods.CMAKE]
                )
                actual = [m() for m in f(env, {'required': False, 'native': MachineChoice.HOST})]
                self.assertListEqual([m.type_name for m in actual], ['pkgconfig', 'cmake'])

                f = F.DependencyFactory(
                    'test_dep',
                    methods=[b.DependencyMethods.CMAKE, b.DependencyMethods.PKGCONFIG]
                )
                actual = [m() for m in f(env, {'required': False, 'native': MachineChoice.HOST})]
                self.assertListEqual([m.type_name for m in actual], ['cmake', 'pkgconfig'])

    def test_validate_json(self) -> None:
        """Validate the json schema for the test cases."""
        try:
            from fastjsonschema import compile, JsonSchemaValueException as JsonSchemaFailure
            fast = True
        except ImportError:
            try:
                from jsonschema import validate, ValidationError as JsonSchemaFailure
                fast = False
            except:
                if IS_CI:
                    raise
                raise unittest.SkipTest('neither Python fastjsonschema nor jsonschema module not found.')

        with open('data/test.schema.json', 'r', encoding='utf-8') as f:
            data = json.loads(f.read())

        if fast:
            schema_validator = compile(data)
        else:
            schema_validator = lambda x: validate(x, schema=data)

        errors: T.List[T.Tuple[Path, Exception]] = []
        for p in Path('test cases').glob('**/test.json'):
            try:
                schema_validator(json.loads(p.read_text(encoding='utf-8')))
            except JsonSchemaFailure as e:
                errors.append((p.resolve(), e))

        for f, e in errors:
            print(f'Failed to validate: "{f}"')
            print(str(e))

        self.assertFalse(errors)

    def test_typed_pos_args_types(self) -> None:
        @typed_pos_args('foo', str, int, bool)
        def _(obj, node, args: T.Tuple[str, int, bool], kwargs) -> None:
            self.assertIsInstance(args, tuple)
            self.assertIsInstance(args[0], str)
            self.assertIsInstance(args[1], int)
            self.assertIsInstance(args[2], bool)

        _(None, mock.Mock(), ['string', 1, False], None)

    def test_typed_pos_args_types_invalid(self) -> None:
        @typed_pos_args('foo', str, int, bool)
        def _(obj, node, args: T.Tuple[str, int, bool], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 1.0, False], None)
        self.assertEqual(str(cm.exception), 'foo argument 2 was of type "float" but should have been "int"')

    def test_typed_pos_args_types_wrong_number(self) -> None:
        @typed_pos_args('foo', str, int, bool)
        def _(obj, node, args: T.Tuple[str, int, bool], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 1], None)
        self.assertEqual(str(cm.exception), 'foo takes exactly 3 arguments, but got 2.')

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 1, True, True], None)
        self.assertEqual(str(cm.exception), 'foo takes exactly 3 arguments, but got 4.')

    def test_typed_pos_args_varargs(self) -> None:
        @typed_pos_args('foo', str, varargs=str)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertIsInstance(args, tuple)
            self.assertIsInstance(args[0], str)
            self.assertIsInstance(args[1], list)
            self.assertIsInstance(args[1][0], str)
            self.assertIsInstance(args[1][1], str)

        _(None, mock.Mock(), ['string', 'var', 'args'], None)

    def test_typed_pos_args_varargs_not_given(self) -> None:
        @typed_pos_args('foo', str, varargs=str)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertIsInstance(args, tuple)
            self.assertIsInstance(args[0], str)
            self.assertIsInstance(args[1], list)
            self.assertEqual(args[1], [])

        _(None, mock.Mock(), ['string'], None)

    def test_typed_pos_args_varargs_invalid(self) -> None:
        @typed_pos_args('foo', str, varargs=str)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 'var', 'args', 0], None)
        self.assertEqual(str(cm.exception), 'foo argument 4 was of type "int" but should have been "str"')

    def test_typed_pos_args_varargs_invalid_multiple_types(self) -> None:
        @typed_pos_args('foo', str, varargs=(str, list))
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 'var', 'args', 0], None)
        self.assertEqual(str(cm.exception), 'foo argument 4 was of type "int" but should have been one of: "str", "list"')

    def test_typed_pos_args_max_varargs(self) -> None:
        @typed_pos_args('foo', str, varargs=str, max_varargs=5)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertIsInstance(args, tuple)
            self.assertIsInstance(args[0], str)
            self.assertIsInstance(args[1], list)
            self.assertIsInstance(args[1][0], str)
            self.assertIsInstance(args[1][1], str)

        _(None, mock.Mock(), ['string', 'var', 'args'], None)

    def test_typed_pos_args_max_varargs_exceeded(self) -> None:
        @typed_pos_args('foo', str, varargs=str, max_varargs=1)
        def _(obj, node, args: T.Tuple[str, T.Tuple[str, ...]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 'var', 'args'], None)
        self.assertEqual(str(cm.exception), 'foo takes between 1 and 2 arguments, but got 3.')

    def test_typed_pos_args_min_varargs(self) -> None:
        @typed_pos_args('foo', varargs=str, max_varargs=2, min_varargs=1)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertIsInstance(args, tuple)
            self.assertIsInstance(args[0], list)
            self.assertIsInstance(args[0][0], str)
            self.assertIsInstance(args[0][1], str)

        _(None, mock.Mock(), ['string', 'var'], None)

    def test_typed_pos_args_min_varargs_not_met(self) -> None:
        @typed_pos_args('foo', str, varargs=str, min_varargs=1)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string'], None)
        self.assertEqual(str(cm.exception), 'foo takes at least 2 arguments, but got 1.')

    def test_typed_pos_args_min_and_max_varargs_exceeded(self) -> None:
        @typed_pos_args('foo', str, varargs=str, min_varargs=1, max_varargs=2)
        def _(obj, node, args: T.Tuple[str, T.Tuple[str, ...]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', 'var', 'args', 'bar'], None)
        self.assertEqual(str(cm.exception), 'foo takes between 2 and 3 arguments, but got 4.')

    def test_typed_pos_args_min_and_max_varargs_not_met(self) -> None:
        @typed_pos_args('foo', str, varargs=str, min_varargs=1, max_varargs=2)
        def _(obj, node, args: T.Tuple[str, T.Tuple[str, ...]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string'], None)
        self.assertEqual(str(cm.exception), 'foo takes between 2 and 3 arguments, but got 1.')

    def test_typed_pos_args_variadic_and_optional(self) -> None:
        @typed_pos_args('foo', str, optargs=[str], varargs=str, min_varargs=0)
        def _(obj, node, args: T.Tuple[str, T.List[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(AssertionError) as cm:
            _(None, mock.Mock(), ['string'], None)
        self.assertEqual(
            str(cm.exception),
            'varargs and optargs not supported together as this would be ambiguous')

    def test_typed_pos_args_min_optargs_not_met(self) -> None:
        @typed_pos_args('foo', str, str, optargs=[str])
        def _(obj, node, args: T.Tuple[str, T.Optional[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string'], None)
        self.assertEqual(str(cm.exception), 'foo takes at least 2 arguments, but got 1.')

    def test_typed_pos_args_min_optargs_max_exceeded(self) -> None:
        @typed_pos_args('foo', str, optargs=[str])
        def _(obj, node, args: T.Tuple[str, T.Optional[str]], kwargs) -> None:
            self.assertTrue(False)  # should not be reachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), ['string', '1', '2'], None)
        self.assertEqual(str(cm.exception), 'foo takes at most 2 arguments, but got 3.')

    def test_typed_pos_args_optargs_not_given(self) -> None:
        @typed_pos_args('foo', str, optargs=[str])
        def _(obj, node, args: T.Tuple[str, T.Optional[str]], kwargs) -> None:
            self.assertEqual(len(args), 2)
            self.assertIsInstance(args[0], str)
            self.assertEqual(args[0], 'string')
            self.assertIsNone(args[1])

        _(None, mock.Mock(), ['string'], None)

    def test_typed_pos_args_optargs_some_given(self) -> None:
        @typed_pos_args('foo', str, optargs=[str, int])
        def _(obj, node, args: T.Tuple[str, T.Optional[str], T.Optional[int]], kwargs) -> None:
            self.assertEqual(len(args), 3)
            self.assertIsInstance(args[0], str)
            self.assertEqual(args[0], 'string')
            self.assertIsInstance(args[1], str)
            self.assertEqual(args[1], '1')
            self.assertIsNone(args[2])

        _(None, mock.Mock(), ['string', '1'], None)

    def test_typed_pos_args_optargs_all_given(self) -> None:
        @typed_pos_args('foo', str, optargs=[str])
        def _(obj, node, args: T.Tuple[str, T.Optional[str]], kwargs) -> None:
            self.assertEqual(len(args), 2)
            self.assertIsInstance(args[0], str)
            self.assertEqual(args[0], 'string')
            self.assertIsInstance(args[1], str)

        _(None, mock.Mock(), ['string', '1'], None)

    def test_typed_kwarg_basic(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', str, default='')
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertIsInstance(kwargs['input'], str)
            self.assertEqual(kwargs['input'], 'foo')

        _(None, mock.Mock(), [], {'input': 'foo'})

    def test_typed_kwarg_missing_required(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', str, required=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertTrue(False)  # should be unreachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), [], {})
        self.assertEqual(str(cm.exception), 'testfunc is missing required keyword argument "input"')

    def test_typed_kwarg_missing_optional(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', (str, type(None))),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.Optional[str]]) -> None:
            self.assertIsNone(kwargs['input'])

        _(None, mock.Mock(), [], {})

    def test_typed_kwarg_default(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', str, default='default'),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertEqual(kwargs['input'], 'default')

        _(None, mock.Mock(), [], {})

    def test_typed_kwarg_container_valid(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str), default=[], required=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.List[str]]) -> None:
            self.assertEqual(kwargs['input'], ['str'])

        _(None, mock.Mock(), [], {'input': ['str']})

    def test_typed_kwarg_container_invalid(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str), required=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.List[str]]) -> None:
            self.assertTrue(False)  # should be unreachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), [], {'input': {}})
        self.assertEqual(str(cm.exception), "testfunc keyword argument 'input' was of type dict[] but should have been array[str]")

    def test_typed_kwarg_contained_invalid(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(dict, str), required=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.Dict[str, str]]) -> None:
            self.assertTrue(False)  # should be unreachable

        with self.assertRaises(InvalidArguments) as cm:
            _(None, mock.Mock(), [], {'input': {'key': 1, 'bar': 2}})
        self.assertEqual(str(cm.exception), "testfunc keyword argument 'input' was of type dict[int] but should have been dict[str]")

    def test_typed_kwarg_container_listify(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str), default=[], listify=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.List[str]]) -> None:
            self.assertEqual(kwargs['input'], ['str'])

        _(None, mock.Mock(), [], {'input': 'str'})

    def test_typed_kwarg_container_default_copy(self) -> None:
        default: T.List[str] = []
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str), listify=True, default=default),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.List[str]]) -> None:
            self.assertIsNot(kwargs['input'], default)

        _(None, mock.Mock(), [], {})

    def test_typed_kwarg_container_pairs(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str, pairs=True), listify=True),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, T.List[str]]) -> None:
            self.assertEqual(kwargs['input'], ['a', 'b'])

        _(None, mock.Mock(), [], {'input': ['a', 'b']})

        with self.assertRaises(MesonException) as cm:
            _(None, mock.Mock(), [], {'input': ['a']})
        self.assertEqual(str(cm.exception), "testfunc keyword argument 'input' was of type array[str] but should have been array[str] that has even size")

    def test_typed_kwarg_since(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', str, since='1.0', since_message='It\'s awesome, use it',
                      deprecated='2.0', deprecated_message='It\'s terrible, don\'t use it')
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertIsInstance(kwargs['input'], str)
            self.assertEqual(kwargs['input'], 'foo')

        with self.subTest('use before available'), \
                mock.patch('sys.stdout', io.StringIO()) as out, \
                mock.patch('mesonbuild.mesonlib.project_meson_versions', {'': version_check_to_range(['>=0.1'])}):
            # With Meson 0.1 it should trigger the "introduced" warning but not the "deprecated" warning
            _(None, mock.Mock(subproject=''), [], {'input': 'foo'})
            self.assertRegex(out.getvalue(), r'WARNING:.*introduced.*input arg in testfunc. It\'s awesome, use it')
            self.assertNotRegex(out.getvalue(), r'WARNING:.*deprecated.*input arg in testfunc. It\'s terrible, don\'t use it')

        with self.subTest('no warnings should be triggered'), \
                mock.patch('sys.stdout', io.StringIO()) as out, \
                mock.patch('mesonbuild.mesonlib.project_meson_versions', {'': version_check_to_range(['>=1.5'])}):
            # With Meson 1.5 it shouldn't trigger any warning
            _(None, mock.Mock(subproject=''), [], {'input': 'foo'})
            self.assertNotRegex(out.getvalue(), r'WARNING:.*')

        with self.subTest('use after deprecated'), \
                mock.patch('sys.stdout', io.StringIO()) as out, \
                mock.patch('mesonbuild.mesonlib.project_meson_versions', {'': version_check_to_range(['>=2.0'])}):
            # With Meson 2.0 it should trigger the "deprecated" warning but not the "introduced" warning
            _(None, mock.Mock(subproject=''), [], {'input': 'foo'})
            self.assertRegex(out.getvalue(), r'WARNING:.*deprecated.*input arg in testfunc. It\'s terrible, don\'t use it')
            self.assertNotRegex(out.getvalue(), r'WARNING:.*introduced.*input arg in testfunc. It\'s awesome, use it')

    def test_typed_kwarg_validator(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', str, default='', validator=lambda x: 'invalid!' if x != 'foo' else None)
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            pass

        # Should be valid
        _(None, mock.Mock(), tuple(), dict(input='foo'))

        with self.assertRaises(MesonException) as cm:
            _(None, mock.Mock(), tuple(), dict(input='bar'))
        self.assertEqual(str(cm.exception), "testfunc keyword argument \"input\" invalid!")

    def test_typed_kwarg_convertor(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('native', bool, default=False, convertor=lambda n: MachineChoice.BUILD if n else MachineChoice.HOST)
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, MachineChoice]) -> None:
            assert isinstance(kwargs['native'], MachineChoice)

        _(None, mock.Mock(), tuple(), dict(native=True))

    @mock.patch('mesonbuild.mesonlib.project_meson_versions', {'': version_check_to_range(['>=1.0'])})
    def test_typed_kwarg_since_values(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', ContainerTypeInfo(list, str), listify=True, default=[], deprecated_values={'foo': '0.9'}, since_values={'bar': '1.1'}),
            KwargInfo('output', ContainerTypeInfo(dict, str), default={}, deprecated_values={'foo': '0.9', 'foo2': ('0.9', 'don\'t use it')}, since_values={'bar': '1.1', 'bar2': ('1.1', 'use this')}),
            KwargInfo('install_dir', (bool, str, NoneType), deprecated_values={False: '0.9'}),
            KwargInfo(
                'mode',
                (str, type(None)),
                validator=in_set_validator({'clean', 'build', 'rebuild', 'deprecated', 'since'}),
                deprecated_values={'deprecated': '1.0'},
                since_values={'since': '1.1'}),
            KwargInfo('dict', (ContainerTypeInfo(list, str), ContainerTypeInfo(dict, str)), default={},
                      since_values={list: '1.9'}),
            KwargInfo('new_dict', (ContainerTypeInfo(list, str), ContainerTypeInfo(dict, str)), default={},
                      since_values={dict: '1.1'}),
            KwargInfo('foo', (str, int, ContainerTypeInfo(list, str), ContainerTypeInfo(dict, str), ContainerTypeInfo(list, int)), default={},
                      since_values={str: '1.1', ContainerTypeInfo(list, str): '1.2', ContainerTypeInfo(dict, str): '1.3'},
                      deprecated_values={int: '0.8', ContainerTypeInfo(list, int): '0.9'}),
            KwargInfo('tuple', (ContainerTypeInfo(list, (str, int))), default=[], listify=True,
                      since_values={ContainerTypeInfo(list, str): '1.1', ContainerTypeInfo(list, int): '1.2'}),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            pass

        with self.subTest('deprecated array string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'input': ['foo']})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "input" value "foo".*""")

        with self.subTest('new array string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'input': ['bar']})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "input" value "bar".*""")

        with self.subTest('deprecated dict string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'output': {'foo': 'a'}})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "output" value "foo".*""")

        with self.subTest('deprecated dict string value with msg'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'output': {'foo2': 'a'}})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "output" value "foo2" in dict keys. don't use it.*""")

        with self.subTest('new dict string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'output': {'bar': 'b'}})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "output" value "bar".*""")

        with self.subTest('new dict string value with msg'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'output': {'bar2': 'a'}})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "output" value "bar2" in dict keys. use this.*""")

        with self.subTest('new string type'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'foo': 'foo'})
            self.assertRegex(out.getvalue(), r"""WARNING: Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "foo" of type str.*""")

        with self.subTest('new array of string type'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'foo': ['foo']})
            self.assertRegex(out.getvalue(), r"""WARNING: Project targets '>= 1.0'.*introduced in '1.2': "testfunc" keyword argument "foo" of type array\[str\].*""")

        with self.subTest('new dict of string type'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'foo': {'plop': 'foo'}})
            self.assertRegex(out.getvalue(), r"""WARNING: Project targets '>= 1.0'.*introduced in '1.3': "testfunc" keyword argument "foo" of type dict\[str\].*""")

        with self.subTest('deprecated int value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'foo': 1})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.8': "testfunc" keyword argument "foo" of type int.*""")

        with self.subTest('deprecated array int value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'foo': [1]})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "foo" of type array\[int\].*""")

        with self.subTest('new list[str] value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'tuple': ['foo', 42]})
            self.assertRegex(out.getvalue(), r"""WARNING: Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "tuple" of type array\[str\].*""")
            self.assertRegex(out.getvalue(), r"""WARNING: Project targets '>= 1.0'.*introduced in '1.2': "testfunc" keyword argument "tuple" of type array\[int\].*""")

        with self.subTest('deprecated array string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'input': 'foo'})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "input" value "foo".*""")

        with self.subTest('new array string value'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'input': 'bar'})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "input" value "bar".*""")

        with self.subTest('non string union'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'install_dir': False})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '0.9': "testfunc" keyword argument "install_dir" value "False".*""")

        with self.subTest('deprecated string union'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'mode': 'deprecated'})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*deprecated since '1.0': "testfunc" keyword argument "mode" value "deprecated".*""")

        with self.subTest('new string union'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'mode': 'since'})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "mode" value "since".*""")

        with self.subTest('new container'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'dict': ['a=b']})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.9': "testfunc" keyword argument "dict" of type list.*""")

        with self.subTest('new container set to default'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {'new_dict': {}})
            self.assertRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "new_dict" of type dict.*""")

        with self.subTest('new container default'), mock.patch('sys.stdout', io.StringIO()) as out:
            _(None, mock.Mock(subproject=''), [], {})
            self.assertNotRegex(out.getvalue(), r"""WARNING:.Project targets '>= 1.0'.*introduced in '1.1': "testfunc" keyword argument "new_dict" of type dict.*""")

    def test_typed_kwarg_evolve(self) -> None:
        k = KwargInfo('foo', str, required=True, default='foo')
        v = k.evolve(default='bar')
        self.assertEqual(k.name, 'foo')
        self.assertEqual(k.name, v.name)
        self.assertEqual(k.types, str)
        self.assertEqual(k.types, v.types)
        self.assertEqual(k.required, True)
        self.assertEqual(k.required, v.required)
        self.assertEqual(k.default, 'foo')
        self.assertEqual(v.default, 'bar')

    def test_typed_kwarg_default_type(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('no_default', (str, ContainerTypeInfo(list, str), NoneType)),
            KwargInfo('str_default', (str, ContainerTypeInfo(list, str)), default=''),
            KwargInfo('list_default', (str, ContainerTypeInfo(list, str)), default=['']),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertEqual(kwargs['no_default'], None)
            self.assertEqual(kwargs['str_default'], '')
            self.assertEqual(kwargs['list_default'], [''])
        _(None, mock.Mock(), [], {})

    def test_typed_kwarg_invalid_default_type(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('invalid_default', (str, ContainerTypeInfo(list, str), NoneType), default=42),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            pass
        self.assertRaises(AssertionError, _, None, mock.Mock(), [], {})

    def test_typed_kwarg_container_in_tuple(self) -> None:
        @typed_kwargs(
            'testfunc',
            KwargInfo('input', (str, ContainerTypeInfo(list, str))),
        )
        def _(obj, node, args: T.Tuple, kwargs: T.Dict[str, str]) -> None:
            self.assertEqual(kwargs['input'], args[0])
        _(None, mock.Mock(), [''], {'input': ''})
        _(None, mock.Mock(), [['']], {'input': ['']})
        self.assertRaises(InvalidArguments, _, None, mock.Mock(), [], {'input': 42})

    def test_detect_cpu_family(self) -> None:
        """Test the various cpu families that we detect and normalize.

        This is particularly useful as both documentation, and to keep testing
        platforms that are less common.
        """

        @contextlib.contextmanager
        def mock_trial(value: str) -> T.Iterable[None]:
            """Mock all of the ways we could get the trial at once."""
            mocked = mock.Mock(return_value=value)

            with mock.patch('mesonbuild.envconfig.detect_windows_arch', mocked), \
                    mock.patch('mesonbuild.envconfig.platform.processor', mocked), \
                    mock.patch('mesonbuild.envconfig.platform.machine', mocked):
                yield

        cases = [
            ('x86', 'x86'),
            ('i386', 'x86'),
            ('bepc', 'x86'),  # Haiku
            ('earm', 'arm'),  # NetBSD
            ('arm', 'arm'),
            ('ppc64', 'ppc64'),
            ('powerpc64', 'ppc64'),
            ('powerpc', 'ppc'),
            ('ppc', 'ppc'),
            ('macppc', 'ppc'),
            ('power macintosh', 'ppc'),
            ('mips64el', 'mips'),
            ('mips64', 'mips'),
            ('mips', 'mips'),
            ('mipsel', 'mips'),
            ('ip30', 'mips'),
            ('ip35', 'mips'),
            ('parisc64', 'parisc'),
            ('sun4u', 'sparc64'),
            ('sun4v', 'sparc64'),
            ('amd64', 'x86_64'),
            ('x64', 'x86_64'),
            ('i86pc', 'x86_64'),  # Solaris
            ('aarch64', 'aarch64'),
            ('aarch64_be', 'aarch64'),
        ]

        cc = ClangCCompiler([], [], 'fake', MachineChoice.HOST, get_fake_env())

        with mock.patch('mesonbuild.envconfig.any_compiler_has_define', mock.Mock(return_value=False)):
            for test, expected in cases:
                with self.subTest(test, has_define=False), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu_family({'c': cc})
                    self.assertEqual(actual, expected)

        with mock.patch('mesonbuild.envconfig.any_compiler_has_define', mock.Mock(return_value=True)):
            for test, expected in [('x86_64', 'x86'), ('aarch64', 'arm'), ('ppc', 'ppc64'), ('mips64', 'mips64')]:
                with self.subTest(test, has_define=True), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu_family({'c': cc})
                    self.assertEqual(actual, expected)

        # machine_info_can_run calls detect_cpu_family with no compilers at all
        with mock.patch(
            'mesonbuild.envconfig.any_compiler_has_define',
            mock.Mock(side_effect=AssertionError('Should not be called')),
        ):
            for test, expected in [('mips64', 'mips64')]:
                with self.subTest(test, has_compiler=False), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu_family({})
                    self.assertEqual(actual, expected)

    def test_detect_cpu(self) -> None:

        @contextlib.contextmanager
        def mock_trial(value: str) -> T.Iterable[None]:
            """Mock all of the ways we could get the trial at once."""
            mocked = mock.Mock(return_value=value)

            with mock.patch('mesonbuild.envconfig.detect_windows_arch', mocked), \
                    mock.patch('mesonbuild.envconfig.platform.processor', mocked), \
                    mock.patch('mesonbuild.envconfig.platform.machine', mocked):
                yield

        cases = [
            ('amd64', 'x86_64'),
            ('x64', 'x86_64'),
            ('i86pc', 'x86_64'),
            ('earm', 'arm'),
            ('mips64el', 'mips'),
            ('mips64', 'mips'),
            ('mips', 'mips'),
            ('mipsel', 'mips'),
            ('aarch64', 'aarch64'),
            ('aarch64_be', 'aarch64'),
        ]

        cc = ClangCCompiler([], [], 'fake', MachineChoice.HOST, get_fake_env())

        with mock.patch('mesonbuild.envconfig.any_compiler_has_define', mock.Mock(return_value=False)):
            for test, expected in cases:
                with self.subTest(test, has_define=False), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu({'c': cc})
                    self.assertEqual(actual, expected)

        with mock.patch('mesonbuild.envconfig.any_compiler_has_define', mock.Mock(return_value=True)):
            for test, expected in [('x86_64', 'i686'), ('aarch64', 'arm'), ('ppc', 'ppc64'), ('mips64', 'mips64')]:
                with self.subTest(test, has_define=True), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu({'c': cc})
                    self.assertEqual(actual, expected)

        with mock.patch(
            'mesonbuild.envconfig.any_compiler_has_define',
            mock.Mock(side_effect=AssertionError('Should not be called')),
        ):
            for test, expected in [('mips64', 'mips64')]:
                with self.subTest(test, has_compiler=False), mock_trial(test):
                    actual = mesonbuild.envconfig.detect_cpu({})
                    self.assertEqual(actual, expected)

    @mock.patch('mesonbuild.interpreter.Interpreter.load_root_meson_file', mock.Mock(return_value=None))
    @mock.patch('mesonbuild.interpreter.Interpreter.sanity_check_ast', mock.Mock(return_value=None))
    @mock.patch('mesonbuild.interpreter.Interpreter.parse_project', mock.Mock(return_value=None))
    def test_interpreter_unpicklable(self) -> None:
        build = mock.Mock()
        build.environment = mock.Mock()
        build.environment.get_source_dir = mock.Mock(return_value='')
        with mock.patch('mesonbuild.interpreter.Interpreter._redetect_machines', mock.Mock()), \
                self.assertRaises(mesonbuild.mesonlib.MesonBugException):
            i = mesonbuild.interpreter.Interpreter(build)
            pickle.dumps(i)

    def test_major_versions_differ(self) -> None:
        # Return True when going to next major release, when going to dev cycle,
        # when going to rc cycle or when going out of rc cycle.
        self.assertTrue(coredata.major_versions_differ('0.59.0', '0.60.0'))
        self.assertTrue(coredata.major_versions_differ('0.59.0', '0.59.99'))
        self.assertTrue(coredata.major_versions_differ('0.59.0', '0.60.0.rc1'))
        self.assertTrue(coredata.major_versions_differ('0.59.99', '0.60.0.rc1'))
        self.assertTrue(coredata.major_versions_differ('0.60.0.rc1', '0.60.0'))
        # Return False when going to next point release or when staying in dev/rc cycle.
        self.assertFalse(coredata.major_versions_differ('0.60.0', '0.60.0'))
        self.assertFalse(coredata.major_versions_differ('0.60.0', '0.60.1'))
        self.assertFalse(coredata.major_versions_differ('0.59.99', '0.59.99'))
        self.assertFalse(coredata.major_versions_differ('0.60.0.rc1', '0.60.0.rc2'))

    def test_option_key_from_string(self) -> None:
        cases = [
            ('c_args', OptionKey('c_args')),
            ('build.cpp_args', OptionKey('cpp_args', machine=MachineChoice.BUILD)),
            ('prefix', OptionKey('prefix')),
            ('made_up', OptionKey('made_up')),

            # TODO: the from_String method should be splitting the prefix off of
            # these, as we have the type already, but it doesn't. For now have a
            # test so that we don't change the behavior un-intentionally
            ('b_lto', OptionKey('b_lto')),
            ('backend_startup_project', OptionKey('backend_startup_project')),
        ]

        for raw, expected in cases:
            with self.subTest(raw):
                self.assertEqual(OptionKey.from_string(raw), expected)

    def test_env2mfile_deb(self) -> None:
        MachineInfo = mesonbuild.scripts.env2mfile.MachineInfo
        to_machine_info = mesonbuild.scripts.env2mfile.dpkg_architecture_to_machine_info

        # For testing purposes, behave as though all cross-programs
        # exist in /usr/bin
        def locate_path(program: str) -> T.List[str]:
            if os.path.isabs(program):
                return [program]
            return ['/usr/bin/' + program]

        def expected_compilers(
            gnu_tuple: str,
            gcc_suffix: str = '',
        ) -> T.Dict[str, T.List[str]]:
            return {
                'c': [f'/usr/bin/{gnu_tuple}-gcc{gcc_suffix}'],
                'cpp': [f'/usr/bin/{gnu_tuple}-g++{gcc_suffix}'],
                'objc': [f'/usr/bin/{gnu_tuple}-gobjc{gcc_suffix}'],
                'objcpp': [f'/usr/bin/{gnu_tuple}-gobjc++{gcc_suffix}'],
                'vala': [f'/usr/bin/{gnu_tuple}-valac'],
            }

        def expected_binaries(gnu_tuple: str) -> T.Dict[str, T.List[str]]:
            return {
                'ar': [f'/usr/bin/{gnu_tuple}-ar'],
                'strip': [f'/usr/bin/{gnu_tuple}-strip'],
                'objcopy': [f'/usr/bin/{gnu_tuple}-objcopy'],
                'ld': [f'/usr/bin/{gnu_tuple}-ld'],
                'cmake': ['/usr/bin/cmake'],
                'pkg-config': [f'/usr/bin/{gnu_tuple}-pkg-config'],
                'cups-config': ['/usr/bin/cups-config'],
                'exe_wrapper': [f'/usr/bin/{gnu_tuple}-cross-exe-wrapper'],
                'g-ir-annotation-tool': [f'/usr/bin/{gnu_tuple}-g-ir-annotation-tool'],
                'g-ir-compiler': [f'/usr/bin/{gnu_tuple}-g-ir-compiler'],
                'g-ir-doc-tool': [f'/usr/bin/{gnu_tuple}-g-ir-doc-tool'],
                'g-ir-generate': [f'/usr/bin/{gnu_tuple}-g-ir-generate'],
                'g-ir-inspect': [f'/usr/bin/{gnu_tuple}-g-ir-inspect'],
                'g-ir-scanner': [f'/usr/bin/{gnu_tuple}-g-ir-scanner'],
                'vapigen': [f'/usr/bin/{gnu_tuple}-vapigen'],
            }

        for title, dpkg_arch, gccsuffix, env, expected in [
            (
                # s390x is an example of the common case where the
                # Meson CPU name, the GNU CPU name, the dpkg architecture
                # name and uname -m all agree.
                # (alpha, m68k, ppc64, riscv64, sh4, sparc64 are similar)
                's390x-linux-gnu',
                # Output of `dpkg-architecture -a...`, filtered to
                # only the DEB_HOST_ parts because that's all we use
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=s390x
                    DEB_HOST_ARCH_ABI=base
                    DEB_HOST_ARCH_BITS=64
                    DEB_HOST_ARCH_CPU=s390x
                    DEB_HOST_ARCH_ENDIAN=big
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=linux
                    DEB_HOST_GNU_CPU=s390x
                    DEB_HOST_GNU_SYSTEM=linux-gnu
                    DEB_HOST_GNU_TYPE=s390x-linux-gnu
                    DEB_HOST_MULTIARCH=s390x-linux-gnu
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('s390x-linux-gnu'),
                    binaries=expected_binaries('s390x-linux-gnu'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/s390x-linux-gnu-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/s390x-linux-gnu-g++'],
                        'CMAKE_SYSTEM_NAME': 'Linux',
                        'CMAKE_SYSTEM_PROCESSOR': 's390x',
                    },
                    system='linux',
                    subsystem='linux',
                    kernel='linux',
                    cpu='s390x',
                    cpu_family='s390x',
                    endian='big',
                ),
            ),
            # Debian amd64 vs. GNU, Meson, etc. x86_64.
            # arm64/aarch64, hppa/parisc, i386/i686/x86, loong64/loongarch64,
            # powerpc/ppc are similar.
            (
                'x86_64-linux-gnu',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=amd64
                    DEB_HOST_ARCH_ABI=base
                    DEB_HOST_ARCH_BITS=64
                    DEB_HOST_ARCH_CPU=amd64
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=linux
                    DEB_HOST_GNU_CPU=x86_64
                    DEB_HOST_GNU_SYSTEM=linux-gnu
                    DEB_HOST_GNU_TYPE=x86_64-linux-gnu
                    DEB_HOST_MULTIARCH=x86_64-linux-gnu
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('x86_64-linux-gnu'),
                    binaries=expected_binaries('x86_64-linux-gnu'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/x86_64-linux-gnu-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/x86_64-linux-gnu-g++'],
                        'CMAKE_SYSTEM_NAME': 'Linux',
                        'CMAKE_SYSTEM_PROCESSOR': 'x86_64',
                    },
                    system='linux',
                    subsystem='linux',
                    kernel='linux',
                    cpu='x86_64',
                    cpu_family='x86_64',
                    endian='little',
                ),
            ),
            (
                'arm-linux-gnueabihf with non-default gcc and environment',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=armhf
                    DEB_HOST_ARCH_ABI=eabihf
                    DEB_HOST_ARCH_BITS=32
                    DEB_HOST_ARCH_CPU=arm
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=linux
                    DEB_HOST_GNU_CPU=arm
                    DEB_HOST_GNU_SYSTEM=linux-gnueabihf
                    DEB_HOST_GNU_TYPE=arm-linux-gnueabihf
                    DEB_HOST_MULTIARCH=arm-linux-gnueabihf
                    '''
                ),
                '-12',
                {
                    'PATH': '/usr/bin',
                    'CPPFLAGS': '-DNDEBUG',
                    'CFLAGS': '-std=c99',
                    'CXXFLAGS': '-std=c++11',
                    'OBJCFLAGS': '-fobjc-exceptions',
                    'OBJCXXFLAGS': '-fobjc-nilcheck',
                    'LDFLAGS': '-Wl,-O1',
                },
                MachineInfo(
                    compilers=expected_compilers('arm-linux-gnueabihf', '-12'),
                    binaries=expected_binaries('arm-linux-gnueabihf'),
                    properties={},
                    compile_args={
                        'c': ['-DNDEBUG', '-std=c99'],
                        'cpp': ['-DNDEBUG', '-std=c++11'],
                        'objc': ['-DNDEBUG', '-fobjc-exceptions'],
                        'objcpp': ['-DNDEBUG', '-fobjc-nilcheck'],
                    },
                    link_args={
                        'c': ['-std=c99', '-Wl,-O1'],
                        'cpp': ['-std=c++11', '-Wl,-O1'],
                        'objc': ['-fobjc-exceptions', '-Wl,-O1'],
                        'objcpp': ['-fobjc-nilcheck', '-Wl,-O1'],
                    },
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/arm-linux-gnueabihf-gcc-12'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/arm-linux-gnueabihf-g++-12'],
                        'CMAKE_SYSTEM_NAME': 'Linux',
                        'CMAKE_SYSTEM_PROCESSOR': 'armv7l',
                    },
                    system='linux',
                    subsystem='linux',
                    kernel='linux',
                    # In a native build this would often be armv8l
                    # (the version of the running CPU) but the architecture
                    # baseline in Debian is officially ARMv7
                    cpu='arm7hlf',
                    cpu_family='arm',
                    endian='little',
                ),
            ),
            (
                'special cases for i386 (i686, x86) and Hurd',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=hurd-i386
                    DEB_HOST_ARCH_ABI=base
                    DEB_HOST_ARCH_BITS=32
                    DEB_HOST_ARCH_CPU=i386
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=hurd
                    DEB_HOST_GNU_CPU=i686
                    DEB_HOST_GNU_SYSTEM=gnu
                    DEB_HOST_GNU_TYPE=i686-gnu
                    DEB_HOST_MULTIARCH=i386-gnu
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('i686-gnu'),
                    binaries=expected_binaries('i686-gnu'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/i686-gnu-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/i686-gnu-g++'],
                        'CMAKE_SYSTEM_NAME': 'GNU',
                        'CMAKE_SYSTEM_PROCESSOR': 'i686',
                    },
                    system='gnu',
                    subsystem='gnu',
                    kernel='gnu',
                    cpu='i686',
                    cpu_family='x86',
                    endian='little',
                ),
            ),
            (
                'special cases for amd64 (x86_64) and kFreeBSD',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=kfreebsd-amd64
                    DEB_HOST_ARCH_ABI=base
                    DEB_HOST_ARCH_BITS=64
                    DEB_HOST_ARCH_CPU=x86_amd64
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=kfreebsd
                    DEB_HOST_GNU_CPU=x86_64
                    DEB_HOST_GNU_SYSTEM=kfreebsd-gnu
                    DEB_HOST_GNU_TYPE=x86_64-kfreebsd-gnu
                    DEB_HOST_MULTIARCH=x86_64-kfreebsd-gnu
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('x86_64-kfreebsd-gnu'),
                    binaries=expected_binaries('x86_64-kfreebsd-gnu'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/x86_64-kfreebsd-gnu-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/x86_64-kfreebsd-gnu-g++'],
                        'CMAKE_SYSTEM_NAME': 'kFreeBSD',
                        'CMAKE_SYSTEM_PROCESSOR': 'x86_64',
                    },
                    system='kfreebsd',
                    subsystem='kfreebsd',
                    kernel='freebsd',
                    cpu='x86_64',
                    cpu_family='x86_64',
                    endian='little',
                ),
            ),
            (
                'special case for mips64el',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=mips64el
                    DEB_HOST_ARCH_ABI=abi64
                    DEB_HOST_ARCH_BITS=64
                    DEB_HOST_ARCH_CPU=mips64el
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=linux
                    DEB_HOST_GNU_CPU=mips64el
                    DEB_HOST_GNU_SYSTEM=linux-gnuabi64
                    DEB_HOST_GNU_TYPE=mips64el-linux-gnuabi64
                    DEB_HOST_MULTIARCH=mips64el-linux-gnuabi64
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('mips64el-linux-gnuabi64'),
                    binaries=expected_binaries('mips64el-linux-gnuabi64'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/mips64el-linux-gnuabi64-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/mips64el-linux-gnuabi64-g++'],
                        'CMAKE_SYSTEM_NAME': 'Linux',
                        'CMAKE_SYSTEM_PROCESSOR': 'mips64',
                    },
                    system='linux',
                    subsystem='linux',
                    kernel='linux',
                    cpu='mips64',
                    cpu_family='mips64',
                    endian='little',
                ),
            ),
            (
                'special case for ppc64el',
                textwrap.dedent(
                    '''
                    DEB_HOST_ARCH=ppc64el
                    DEB_HOST_ARCH_ABI=base
                    DEB_HOST_ARCH_BITS=64
                    DEB_HOST_ARCH_CPU=ppc64el
                    DEB_HOST_ARCH_ENDIAN=little
                    DEB_HOST_ARCH_LIBC=gnu
                    DEB_HOST_ARCH_OS=linux
                    DEB_HOST_GNU_CPU=powerpc64le
                    DEB_HOST_GNU_SYSTEM=linux-gnu
                    DEB_HOST_GNU_TYPE=powerpc64le-linux-gnu
                    DEB_HOST_MULTIARCH=powerpc64le-linux-gnu
                    '''
                ),
                '',
                {'PATH': '/usr/bin'},
                MachineInfo(
                    compilers=expected_compilers('powerpc64le-linux-gnu'),
                    binaries=expected_binaries('powerpc64le-linux-gnu'),
                    properties={},
                    compile_args={},
                    link_args={},
                    cmake={
                        'CMAKE_C_COMPILER': ['/usr/bin/powerpc64le-linux-gnu-gcc'],
                        'CMAKE_CXX_COMPILER': ['/usr/bin/powerpc64le-linux-gnu-g++'],
                        'CMAKE_SYSTEM_NAME': 'Linux',
                        'CMAKE_SYSTEM_PROCESSOR': 'ppc64le',
                    },
                    system='linux',
                    subsystem='linux',
                    kernel='linux',
                    # TODO: Currently ppc64, but native builds have ppc64le
                    # https://github.com/mesonbuild/meson/issues/13741
                    cpu='TODO',
                    cpu_family='ppc64',
                    endian='little',
                ),
            ),
        ]:
            with self.subTest(title), \
                    unittest.mock.patch.dict('os.environ', env, clear=True), \
                    unittest.mock.patch('mesonbuild.scripts.env2mfile.locate_path') as mock_locate_path:
                mock_locate_path.side_effect = locate_path
                options = argparse.Namespace()
                options.gccsuffix = gccsuffix
                actual = to_machine_info(dpkg_arch, options)

                if expected.system == 'TODO':
                    print(f'TODO: {title}: system() -> {actual.system}')
                else:
                    self.assertEqual(actual.system, expected.system)

                if expected.subsystem == 'TODO':
                    print(f'TODO: {title}: subsystem() -> {actual.subsystem}')
                else:
                    self.assertEqual(actual.subsystem, expected.subsystem)

                if expected.kernel == 'TODO':
                    print(f'TODO: {title}: kernel() -> {actual.kernel}')
                else:
                    self.assertEqual(actual.kernel, expected.kernel)

                if expected.cpu == 'TODO':
                    print(f'TODO: {title}: cpu() -> {actual.cpu}')
                else:
                    self.assertEqual(actual.cpu, expected.cpu)

                self.assertEqual(actual.cpu_family, expected.cpu_family)
                self.assertEqual(actual.endian, expected.endian)

                self.assertEqual(actual.compilers, expected.compilers)
                self.assertEqual(actual.binaries, expected.binaries)
                self.assertEqual(actual.properties, expected.properties)
                self.assertEqual(actual.compile_args, expected.compile_args)
                self.assertEqual(actual.link_args, expected.link_args)
                self.assertEqual(actual.cmake, expected.cmake)
