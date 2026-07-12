# SPDX-License-Identifier: Apache-2.0
# Copyright 2016-2021 The Meson development team

import subprocess
import re
import os
import json
import shutil
import tempfile
from unittest import mock, SkipTest, skipUnless, skipIf
from glob import glob

import mesonbuild.mlog
import mesonbuild.depfile
import mesonbuild.dependencies.factory
import mesonbuild.envconfig
import mesonbuild.environment
import mesonbuild.coredata
import mesonbuild.modules.gnome
from mesonbuild.mesonlib import (
    MachineChoice, is_windows, is_cygwin, python_command, version_compare,
    EnvironmentException
)
from mesonbuild.options import OptionKey
from mesonbuild.compilers import (
    detect_c_compiler, detect_d_compiler, compiler_from_language,
)
from mesonbuild.programs import ExternalProgram
import mesonbuild.dependencies.base
import mesonbuild.modules.pkgconfig


from run_tests import (
    Backend, get_fake_env
)

from .baseplatformtests import BasePlatformTests
from .cppmodules import CppModulesTestMixin, requires_cpp_module_caps
from .helpers import *

@skipUnless(is_windows() or is_cygwin(), "requires Windows (or Windows via Cygwin)")
class WindowsTests(CppModulesTestMixin, BasePlatformTests):
    '''
    Tests that should run on Cygwin, MinGW, and MSVC
    '''

    def setUp(self):
        super().setUp()
        self.platform_test_dir = os.path.join(self.src_root, 'test cases/windows')

    @skipIf(is_cygwin(), 'Test only applicable to Windows')
    @mock.patch.dict(os.environ)
    def test_find_program(self):
        '''
        Test that Windows-specific edge-cases in find_program are functioning
        correctly. Cannot be an ordinary test because it involves manipulating
        PATH to point to a directory with Python scripts.
        '''
        testdir = os.path.join(self.platform_test_dir, '8 find program')
        # Find `cmd` and `cmd.exe`
        prog1 = ExternalProgram('cmd')
        self.assertTrue(prog1.found(), msg='cmd not found')
        prog2 = ExternalProgram('cmd.exe')
        self.assertTrue(prog2.found(), msg='cmd.exe not found')
        self.assertPathEqual(prog1.get_path(), prog2.get_path())
        # Find cmd.exe with args without searching
        prog = ExternalProgram('cmd', command=['cmd', '/C'])
        self.assertTrue(prog.found(), msg='cmd not found with args')
        self.assertPathEqual(prog.get_command()[0], 'cmd')
        # Find cmd with an absolute path that's missing the extension
        cmd_path = prog2.get_path()[:-4]
        prog = ExternalProgram(cmd_path)
        self.assertTrue(prog.found(), msg=f'{cmd_path!r} not found')
        # Finding a script with no extension inside a directory works
        prog = ExternalProgram(os.path.join(testdir, 'test-script'))
        self.assertTrue(prog.found(), msg='test-script not found')
        # Finding a script with an extension inside a directory works
        prog = ExternalProgram(os.path.join(testdir, 'test-script-ext.py'))
        self.assertTrue(prog.found(), msg='test-script-ext.py not found')
        # Finding a script in PATH
        os.environ['PATH'] += os.pathsep + testdir
        # If `.PY` is in PATHEXT, scripts can be found as programs
        if '.PY' in [ext.upper() for ext in os.environ['PATHEXT'].split(';')]:
            # Finding a script in PATH w/o extension works and adds the interpreter
            prog = ExternalProgram('test-script-ext')
            self.assertTrue(prog.found(), msg='test-script-ext not found in PATH')
            self.assertPathEqual(prog.get_command()[0], python_command[0])
            self.assertPathBasenameEqual(prog.get_path(), 'test-script-ext.py')
        # Finding a script in PATH with extension works and adds the interpreter
        prog = ExternalProgram('test-script-ext.py')
        self.assertTrue(prog.found(), msg='test-script-ext.py not found in PATH')
        self.assertPathEqual(prog.get_command()[0], python_command[0])
        self.assertPathBasenameEqual(prog.get_path(), 'test-script-ext.py')
        # Using a script with an extension directly via command= works and adds the interpreter
        prog = ExternalProgram('test-script-ext.py', command=[os.path.join(testdir, 'test-script-ext.py'), '--help'])
        self.assertTrue(prog.found(), msg='test-script-ext.py with full path not picked up via command=')
        self.assertPathEqual(prog.get_command()[0], python_command[0])
        self.assertPathEqual(prog.get_command()[2], '--help')
        self.assertPathBasenameEqual(prog.get_path(), 'test-script-ext.py')
        # Using a script without an extension directly via command= works and adds the interpreter
        prog = ExternalProgram('test-script', command=[os.path.join(testdir, 'test-script'), '--help'])
        self.assertTrue(prog.found(), msg='test-script with full path not picked up via command=')
        self.assertPathEqual(prog.get_command()[0], python_command[0])
        self.assertPathEqual(prog.get_command()[2], '--help')
        self.assertPathBasenameEqual(prog.get_path(), 'test-script')
        # Ensure that WindowsApps gets removed from PATH
        path = os.environ['PATH']
        if 'WindowsApps' not in path:
            username = os.environ['USERNAME']
            appstore_dir = fr'C:\Users\{username}\AppData\Local\Microsoft\WindowsApps'
            path = os.pathsep + appstore_dir
        path = ExternalProgram._windows_sanitize_path(path)
        self.assertNotIn('WindowsApps', path)

    def test_ignore_libs(self):
        '''
        Test that find_library on libs that are to be ignored returns an empty
        array of arguments. Must be a unit test because we cannot inspect
        ExternalLibraryHolder from build files.
        '''
        testdir = os.path.join(self.platform_test_dir, '1 basic')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_argument_syntax() != 'msvc':
            raise SkipTest('Not using MSVC')
        # To force people to update this test, and also test
        self.assertEqual(set(cc.ignore_libs), {'c', 'm', 'pthread', 'dl', 'rt', 'execinfo'})
        for l in cc.ignore_libs:
            self.assertEqual(cc.find_library(l, env, []), [])

    def test_rc_depends_files(self):
        testdir = os.path.join(self.platform_test_dir, '5 resources')

        # resource compiler depfile generation is not yet implemented for msvc
        env = get_fake_env(testdir, self.builddir, self.prefix)
        depfile_works = detect_c_compiler(env, MachineChoice.HOST).get_id() not in {'msvc', 'clang-cl', 'intel-cl'}

        self.init(testdir)
        self.build()
        # Immediately rebuilding should not do anything
        self.assertBuildIsNoop()
        # Test compile_resources(depend_file:)
        # Changing mtime of sample.ico should rebuild prog
        self.utime(os.path.join(testdir, 'res', 'sample.ico'))
        self.assertRebuiltTarget('prog')
        # Test depfile generation by compile_resources
        # Changing mtime of resource.h should rebuild myres.rc and then prog
        if depfile_works:
            self.utime(os.path.join(testdir, 'inc', 'resource', 'resource.h'))
            self.assertRebuiltTarget('prog')
        self.wipe()

        if depfile_works:
            testdir = os.path.join(self.platform_test_dir, '12 resources with custom targets')
            self.init(testdir)
            self.build()
            # Immediately rebuilding should not do anything
            self.assertBuildIsNoop()
            # Changing mtime of resource.h should rebuild myres_1.rc and then prog_1
            self.utime(os.path.join(testdir, 'res', 'resource.h'))
            self.assertRebuiltTarget('prog_1')

    def test_msvc_cpp17(self):
        testdir = os.path.join(self.unit_test_dir, '44 vscpp17')

        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_argument_syntax() != 'msvc':
            raise SkipTest('Test only applies to MSVC-like compilers')

        try:
            self.init(testdir)
        except subprocess.CalledProcessError:
            # According to Python docs, output is only stored when
            # using check_output. We don't use it, so we can't check
            # that the output is correct (i.e. that it failed due
            # to the right reason).
            return
        self.build()

    @skipIf(is_cygwin(), 'Test only applicable to Windows')
    def test_genvslite(self):
        # The test framework itself might be forcing a specific, non-ninja backend across a set of tests, which
        # includes this test. E.g. -
        #   > python.exe run_unittests.py --backend=vs WindowsTests
        # Since that explicitly specifies a backend that's incompatible with (and essentially meaningless in
        # conjunction with) 'genvslite', we should skip further genvslite testing.
        if self.backend is not Backend.ninja:
            raise SkipTest('Test only applies when using the Ninja backend')

        testdir = os.path.join(self.unit_test_dir, '118 genvslite')

        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_argument_syntax() != 'msvc':
            raise SkipTest('Test only applies when MSVC tools are available.')

        # We want to run the genvslite setup. I.e. -
        #    meson setup --genvslite vs2022 ...
        # which we should expect to generate the set of _debug/_debugoptimized/_release suffixed
        # build directories.  Then we want to check that the solution/project build hooks (like clean,
        # build, and rebuild) end up ultimately invoking the 'meson compile ...' of the appropriately
        # suffixed build dir, for which we need to use 'msbuild.exe'

        # Find 'msbuild.exe'
        msbuildprog = ExternalProgram('msbuild.exe')
        self.assertTrue(msbuildprog.found(), msg='msbuild.exe not found')

        # Setup with '--genvslite ...'
        self.new_builddir()

        # Firstly, we'd like to check that meson errors if the user explicitly specifies a non-ninja backend
        # during setup.
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.init(testdir, extra_args=['--genvslite', 'vs2022', '--backend', 'vs'])
        self.assertIn("specifying a non-ninja backend conflicts with a 'genvslite' setup", cm.exception.stdout)

        # Wrap the following bulk of setup and msbuild invocation testing in a try-finally because any exception,
        # failure, or success must always clean up any of the suffixed build dir folders that may have been generated.
        try:
            # Since this
            self.init(testdir, extra_args=['--genvslite', 'vs2022'])
            # We need to bear in mind that the BasePlatformTests framework creates and cleans up its own temporary
            # build directory.  However, 'genvslite' creates a set of suffixed build directories which we'll have
            # to clean up ourselves. See 'finally' block below.

            # We intentionally skip the -
            #   self.build()
            # step because we're wanting to test compilation/building through the solution/project's interface.

            # Execute the debug and release builds through the projects 'Build' hooks
            genvslite_vcxproj_path = str(os.path.join(self.builddir+'_vs', 'genvslite@exe.vcxproj'))
            # This use-case of invoking the .sln/.vcxproj build hooks, not through Visual Studio itself, but through
            # 'msbuild.exe', in a VS tools command prompt environment (e.g. "x64 Native Tools Command Prompt for VS 2022"), is a
            # problem:  Such an environment sets the 'VSINSTALLDIR' variable which, mysteriously, has the side-effect of causing
            # the spawned 'meson compile' command to fail to find 'ninja' (and even when ninja can be found elsewhere, all the
            # compiler binaries that ninja wants to run also fail to be found).  The PATH environment variable in the child python
            # (and ninja) processes are fundamentally stripped down of all the critical search paths required to run the ninja
            # compile work ... ONLY when 'VSINSTALLDIR' is set;  without 'VSINSTALLDIR' set, the meson compile command does search
            # for and find ninja (ironically, it finds it under the path where VSINSTALLDIR pointed!).
            # For the above reason, this testing works around this bizarre behaviour by temporarily removing any 'VSINSTALLDIR'
            # variable, prior to invoking the builds -
            current_env = os.environ.copy()
            current_env.pop('VSINSTALLDIR', None)
            subprocess.check_call(
                ['msbuild', '-target:Build', '-property:Configuration=debug', genvslite_vcxproj_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=current_env)
            subprocess.check_call(
                ['msbuild', '-target:Build', '-property:Configuration=release', genvslite_vcxproj_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=current_env)

            # Check this has actually built the appropriate exes
            exe_path = str(os.path.join(self.builddir+'_debug', 'genvslite.exe'))
            self.assertTrue(os.path.exists(exe_path))
            rc = subprocess.run([exe_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
            output_debug = rc.stdout
            self.assertEqual(output_debug, b'Debug\r\n' )
            exe_path = str(os.path.join(self.builddir+'_release', 'genvslite.exe'))
            self.assertTrue(os.path.exists(exe_path))
            output_release = subprocess.check_output([exe_path])
            self.assertEqual( output_release, b'Non-debug\r\n' )

        finally:
            # Clean up our special suffixed temporary build dirs
            suffixed_build_dirs = glob(self.builddir+'_*', recursive=False)
            for build_dir in suffixed_build_dirs:
                shutil.rmtree(build_dir)

    def test_install_pdb_introspection(self):
        testdir = os.path.join(self.platform_test_dir, '1 basic')

        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_argument_syntax() != 'msvc':
            raise SkipTest('Test only applies to MSVC-like compilers')

        self.init(testdir)
        installed = self.introspect('--installed')
        files = [os.path.basename(path) for path in installed.values()]

        self.assertIn('prog.pdb', files)

    def _check_ld(self, name: str, lang: str, expected: str) -> None:
        if not shutil.which(name):
            raise SkipTest(f'Could not find {name}.')
        envvars = mesonbuild.envconfig.ENV_VAR_PROG_MAP[f'{lang}_ld'].copy()

        # Also test a deprecated variable if there is one.
        if f'{lang}_ld' in mesonbuild.envconfig.DEPRECATED_ENV_PROG_MAP:
            envvars.extend(
                mesonbuild.envconfig.DEPRECATED_ENV_PROG_MAP[f'{lang}_ld'])

        for envvar in envvars:
            with mock.patch.dict(os.environ, {envvar: name}):
                env = get_fake_env()
                try:
                    comp = compiler_from_language(env, lang, MachineChoice.HOST)
                except EnvironmentException:
                    raise SkipTest(f'Could not find a compiler for {lang}')
                self.assertEqual(comp.linker.id, expected)

    def test_link_environment_variable_lld_link(self):
        env = get_fake_env()
        comp = detect_c_compiler(env, MachineChoice.HOST)
        if comp.get_argument_syntax() == 'gcc':
            raise SkipTest('GCC cannot be used with link compatible linkers.')
        self._check_ld('lld-link', 'c', 'lld-link')

    def test_link_environment_variable_link(self):
        env = get_fake_env()
        comp = detect_c_compiler(env, MachineChoice.HOST)
        if comp.get_argument_syntax() == 'gcc':
            raise SkipTest('GCC cannot be used with link compatible linkers.')
        self._check_ld('link', 'c', 'link')

    def test_link_environment_variable_optlink(self):
        env = get_fake_env()
        comp = detect_c_compiler(env, MachineChoice.HOST)
        if comp.get_argument_syntax() == 'gcc':
            raise SkipTest('GCC cannot be used with link compatible linkers.')
        self._check_ld('optlink', 'c', 'optlink')

    @skip_if_not_language('rust')
    def test_link_environment_variable_rust(self):
        self._check_ld('link', 'rust', 'link')

    @skip_if_not_language('d')
    def test_link_environment_variable_d(self):
        env = get_fake_env()
        comp = detect_d_compiler(env, MachineChoice.HOST)
        if comp.id == 'dmd':
            raise SkipTest('meson cannot reliably make DMD use a different linker.')
        self._check_ld('lld-link', 'd', 'lld-link')

    def test_pefile_checksum(self):
        try:
            import pefile
        except ImportError:
            if IS_CI:
                raise
            raise SkipTest('pefile module not found')
        testdir = os.path.join(self.common_test_dir, '6 linkshared')
        self.init(testdir, extra_args=['--buildtype=release'])
        self.build()
        # Test that binaries have a non-zero checksum
        env = get_fake_env()
        cc = detect_c_compiler(env, MachineChoice.HOST)
        cc_id = cc.get_id()
        ld_id = cc.get_linker_id()
        dll = glob(os.path.join(self.builddir, '*mycpplib.dll'))[0]
        exe = os.path.join(self.builddir, 'cppprog.exe')
        for f in (dll, exe):
            pe = pefile.PE(f)
            msg = f'PE file: {f!r}, compiler: {cc_id!r}, linker: {ld_id!r}'
            if cc_id == 'clang-cl':
                # Latest clang-cl tested (7.0) does not write checksums out
                self.assertFalse(pe.verify_checksum(), msg=msg)
            else:
                # Verify that a valid checksum was written by all other compilers
                self.assertTrue(pe.verify_checksum(), msg=msg)

    @skip_if_not_base_option('b_vscrt')
    def test_qt5dependency_vscrt(self):
        '''
        Test that qt5 dependencies use the debug module suffix when b_vscrt is
        set to 'mdd'
        '''
        # Verify that qmake is for Qt5
        if not shutil.which('qmake-qt5'):
            if not IS_CI and not shutil.which('qmake'):
                raise SkipTest('QMake not found')
            output = subprocess.getoutput('qmake --version')
            if not IS_CI and 'Qt version 5' not in output:
                raise SkipTest('Qmake found, but it is not for Qt 5.')
        # Setup with /MDd
        testdir = os.path.join(self.framework_test_dir, '4 qt')
        self.init(testdir, extra_args=['-Db_vscrt=mdd'])
        # Verify that we're linking to the debug versions of Qt DLLs
        build_ninja = os.path.join(self.builddir, 'build.ninja')
        with open(build_ninja, encoding='utf-8') as f:
            contents = f.read()
            m = re.search('build qt5core.exe: cpp_LINKER.*Qt5Cored.lib', contents)
        self.assertIsNotNone(m, msg=contents)

    @skip_if_not_base_option('b_vscrt')
    def test_compiler_checks_vscrt(self):
        '''
        Test that the correct VS CRT is used when running compiler checks
        '''
        env = get_fake_env()
        cc = detect_c_compiler(env, MachineChoice.HOST)

        MSVCRT_MAP = {
            '/MD': '-fms-runtime-lib=dll',
            '/MDd': '-fms-runtime-lib=dll_dbg',
            '/MT': '-fms-runtime-lib=static',
            '/MTd': '-fms-runtime-lib=static_dbg',
        }

        def sanitycheck_vscrt(vscrt):
            if cc.get_argument_syntax() != 'msvc':
                vscrt = MSVCRT_MAP[vscrt]
            checks = self.get_meson_log_sanitychecks()
            self.assertGreater(len(checks), 0)
            for check in checks:
                self.assertIn(vscrt, check)

        testdir = os.path.join(self.common_test_dir, '1 trivial')
        self.init(testdir)
        sanitycheck_vscrt('/MDd')

        self.new_builddir()
        self.init(testdir, extra_args=['-Dbuildtype=debugoptimized'])
        sanitycheck_vscrt('/MD')

        self.new_builddir()
        self.init(testdir, extra_args=['-Dbuildtype=release'])
        sanitycheck_vscrt('/MD')

        self.new_builddir()
        self.init(testdir, extra_args=['-Db_vscrt=md'])
        sanitycheck_vscrt('/MD')

        self.new_builddir()
        self.init(testdir, extra_args=['-Db_vscrt=mdd'])
        sanitycheck_vscrt('/MDd')

        self.new_builddir()
        self.init(testdir, extra_args=['-Db_vscrt=mt'])
        sanitycheck_vscrt('/MT')

        self.new_builddir()
        self.init(testdir, extra_args=['-Db_vscrt=mtd'])
        sanitycheck_vscrt('/MTd')

    def test_modules(self):
        if self.backend is not Backend.ninja:
            raise SkipTest(f'C++ modules only work with the Ninja backend (not {self.backend.name}).')
        if 'VSCMD_VER' not in os.environ:
            raise SkipTest('C++ modules is only supported with Visual Studio.')
        if version_compare(os.environ['VSCMD_VER'], '<16.10.0'):
            raise SkipTest('C++ modules are only supported with VS 2019 Preview or newer.')
        self.init(os.path.join(self.unit_test_dir, '85 cpp modules'))
        self.build()

    @requires_cpp_module_caps('modules', 'partitions', compiler='msvc')
    def test_msvc_cpp_modules(self):
        # The library provides a module, imported by an executable that merely
        # links it, plus partitions, an explicit-opt-in target, and a generated
        # interface; each test() exercises a producer/consumer pair.
        self.build_and_check_modules('149 msvc cpp modules',
                                     bmis=['modlib', 'pkg', 'pkg:part', 'pkg:impl',
                                           'kwmod', 'genmod', 'dot.mod.sub'])
        # A single-class build keeps the flat ifc.cache (bmis= above asserted
        # the flat BMI paths) and plain compile edges: module-resolution args
        # ride /ifcSearchDir, so nothing pushes a compile into a response file.
        self.assertEqual(self.bmi_class_dirs(), [])
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build '):
                    self.assertNotIn('COMPILER_RSP', line)

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='msvc')
    def test_msvc_cpp_module_interfaces(self):
        # A .cc source declared a module interface via cpp_module_interfaces gets
        # /interface and its BMI lands in the shared cache under the module name.
        self.build_and_check_modules('162 cpp module interfaces',
                                     bmis=['mymod', 'filemod'])

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_module_rebuild_on_interface_change(self):
        self.check_module_rebuild('149 msvc cpp modules', edit_file='modlib.ixx')

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='msvc')
    def test_msvc_cpp_module_graph_mutation(self):
        self.check_module_graph_mutation('164 cpp module graph mutation')

    @requires_cpp_module_caps('modules', 'import_std', compiler='msvc')
    def test_msvc_import_std(self):
        # `import std;` / `import std.compat;` resolved via dependency('std'),
        # which synthesizes one static library carrying both std module objects
        # and puts it on the link line of every consumer -- including a target
        # that only links a std-importing library without importing std itself.
        self.build_and_check_modules('150 msvc import std',
                                     bmis=['std', 'std.compat'])

    @requires_cpp_module_caps('modules', 'header_units', compiler='msvc')
    def test_msvc_header_units(self):
        # A user unit, a system unit and a named module in one target; a second
        # target reimports the user unit. Building + running proves the units
        # are pre-built, mapped onto the consumers, and ordered before the scan.
        # cl names unit BMIs on consumer command lines (as /headerUnit
        # mappings), so the default ARGS check is off; the bespoke scan below
        # enforces the sharper rule.
        # setup_not_contains: prog and prog2 declare the same unit in the same
        # BMI class, which must stay warning-free.
        self.build_and_check_modules('151 msvc header units',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        # Units are pre-built to a Meson-chosen .ifc, deduped globally by
        # (mode, spelling): the user unit shared by both targets is one edge/BMI.
        hudir = os.path.join(self.builddir, 'meson-private', 'header-units')
        self.assertEqual(len(glob(os.path.join(hudir, 'util.h.*.ifc'))), 1)
        self.assertEqual(len(glob(os.path.join(hudir, 'angleutil.h.*.ifc'))), 1)
        # cl has no directory lookup for header units, so the consumer compile
        # does name the unit .ifc -- but only as a /headerUnit mapping; a named
        # module BMI never appears on a command line, nor does /reference.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                self.assertNotIn('/reference', line)
                if line.strip().startswith('ARGS =') and '.ifc' in line:
                    self.assertIn('/headerUnit', line)

    @requires_cpp_module_caps('modules', 'header_units', compiler='msvc')
    def test_msvc_undeclared_header_unit(self):
        testdir = os.path.join(self.unit_test_dir, '152 msvc undeclared header unit')
        # A source imports a header unit the target never declared; the collator
        # must fail with a clear error rather than letting the compile fail with
        # a bare C7612.
        self.init(testdir)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('not declared in this target', cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'header_units', compiler='msvc')
    def test_msvc_header_unit_aliasing(self):
        # The same header imported from TUs in different directories. With the
        # include-path spelling every importer resolves the one declared unit:
        # exactly one unit edge serves them all.
        testdir = '173 header unit aliasing'
        self.build_and_check_modules(testdir, ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_digests('header.hpp')), 1)
        # cl matches a header unit by the *literal* import spelling it scans,
        # not a normalized or resolved path, so an importer-relative
        # "../header.hpp" matches no target-level declaration. Unlike GCC there
        # is no alias escape hatch: the GCC-style resolved-path spelling
        # (foo/../header.hpp, mode 'aliased') still mismatches the scanned
        # "../header.hpp", and the bare spelling itself cl cannot open as a
        # file. Both fail at Meson's collator with a legible message; only the
        # include-path spelling (plain) is portable.
        # Grouping (declared spellings of one file sharing a BMI) is emitted at
        # configure time, before cl ever gets a chance to reject the import, so
        # it is observable here even though 'aliased'/'classes' still fail at
        # the collator: exactly one unit edge per BMI class, not one per
        # spelling.
        for mode in ('aliased', 'undeclared', 'classes'):
            with self.subTest(mode=mode):
                self.new_builddir()
                self.init(os.path.join(self.unit_test_dir, testdir),
                          extra_args=[f'-Dmode={mode}'])
                if mode in ('aliased', 'classes'):
                    self.assertEqual(len(self.header_unit_bmis('header.hpp')),
                                     2 if mode == 'classes' else 1)
                with self.assertRaises(subprocess.CalledProcessError) as cm:
                    self.build()
                self.assertIn('not declared in this target', cm.exception.stdout)

    # MSVC has no cpp_std=c++23/c++26, so the shared two-class fixtures (which
    # default to c++23 parents) are driven at c++latest/c++20 instead; the
    # class split is the same. Subproject dialects are pinned explicitly
    # rather than left to the subproject's default_options, so the tests do
    # not depend on how command-line options and subproject defaults interact.
    _MSVC_TWO_CLASS_ARGS = ['-Dcpp_std=c++latest', '-Dmodlib:cpp_std=c++20']

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_bmi_classes(self):
        # The canonical two-class fixture: subproject provider at c++20,
        # consumers at c++latest (variant) and c++20 (reuse).
        self.check_bmi_classes('166 bmi classes', module_name='modlib',
                               provider_lib='libmodlib.a',
                               consumers=('prog23.exe', 'prog20.exe'),
                               expected_targets=('modlib', 'prog20', 'prog23'),
                               extra_args=self._MSVC_TWO_CLASS_ARGS)

    @requires_cpp_module_caps('modules', 'import_std', 'bmi_classes', compiler='msvc')
    def test_msvc_import_std_bmi_classes(self):
        # import std at two dialects (c++20 base, c++latest divergent; cl
        # supports import std from /std:c++20). One __meson_cxx_std target
        # carries the only std objects -- its std.obj is real code on MSVC,
        # so a duplicate would be a genuine ODR violation, not a near-empty
        # initializer. compat_in_all_classes: cl writes the provider class's
        # BMIs eagerly via directory /ifcOutput.
        self.check_import_std_bmi_classes('167 import std bmi classes',
                                          progs=('prog23.exe', 'prog26.exe'),
                                          compat_progs=('prog26.exe',),
                                          compat_in_all_classes=True,
                                          extra_args=['-Dcpp_std=c++20',
                                                      '-Ddivergent_std=c++latest'])

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='msvc')
    def test_msvc_header_unit_bmi_classes(self):
        # Three declarers of util.h in two dialects: the same-class pair must
        # share one unit BMI, the divergent declarer gets its own, each
        # consumer names only its own class's, and modlib's BMI-only variant
        # imports the divergent class's unit, not the provider's. Every
        # program constant-evaluates the unit's dialect probe, so a wrongly
        # shared BMI is a failing test run, not merely a build failure.
        self.build_and_check_modules('169 header unit bmi classes',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=(),
                                     extra_args=self._MSVC_TWO_CLASS_ARGS)
        units = self.header_unit_digests('util.h')
        self.assertEqual(len(units), 2, f'expected one util.h BMI per class, got {units}')
        per_prog = {p: self.header_unit_digests('util.h', edges=f'{p}.exe.p/')
                    for p in ('prog23', 'prog20', 'prog20b')}
        for prog, digests in per_prog.items():
            self.assertEqual(len(digests), 1, f'{prog} must name exactly one util.h BMI, got {digests}')
        self.assertEqual(per_prog['prog20'], per_prog['prog20b'],
                         'same-class declarers must share one unit BMI')
        self.assertNotEqual(per_prog['prog23'], per_prog['prog20'],
                            'divergent classes must not share a unit BMI')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        self.assertEqual(self.header_unit_digests('util.h', edges='@bmi@'), per_prog['prog23'],
                         "modlib's variant must import the divergent class's unit BMI")

    @requires_cpp_module_caps('modules', 'partitions', 'bmi_classes', compiler='msvc')
    def test_msvc_module_internal_partitions(self):
        # An internal (implementation) partition on MSVC: `module pkg:impl;` has
        # no export yet provides an importable BMI. cl rejects an interface file
        # extension for it (C7622) and needs /internalPartition, not /interface,
        # so it is a plain .cpp declared via cpp_internal_partitions; :impl also
        # imports :part (partition-to-partition requires). The divergent
        # consumer's BMI-only variant must recompile the internal partition too
        # -- the constexpr both consumers compare against their own flags lives
        # in :impl, behind two imports.
        self.build_and_check_modules('175 msvc module internal partitions')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        # The scan reports the partition as a non-interface provide; the
        # pipeline must build its BMI all the same.
        with open(os.path.join(self.builddir, 'libpkg.a.p', 'pkg-impl.cpp.obj.ddi'),
                  encoding='utf-8') as f:
            provides = json.load(f)['rules'][0]['provides']
        self.assertEqual([(p['logical-name'], p['is-interface']) for p in provides],
                         [('pkg:impl', False)])
        # cl flags the internal partition /internalPartition rather than
        # /interface (compiling it as an interface is C7621/C7622, so the build
        # above already proves the split; '/interface' is not a substring of
        # '/internalPartition'). Every edge that names pkg-impl.cpp -- compile,
        # scan and BMI-only variant -- must carry /internalPartition and never
        # /interface, so scan and compile keep dialect parity across the split.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            ninja = f.read()
        self.assertIn('/internalPartition', ninja)
        saw_internal_partition = False
        block = []
        for line in ninja.splitlines() + ['end']:
            if not line or not line[0].isspace():
                if any(b.startswith('build ') and 'pkg-impl.cpp' in b for b in block):
                    args = ' '.join(block)
                    # No pkg-impl edge (compile, scan, variant) may flag it an
                    # interface; the collate/harvest edges name it too but carry
                    # no interface-kind flag. '/interface' is not a substring of
                    # '/internalPartition' or '--interface-source'.
                    self.assertNotIn('/interface', args)
                    saw_internal_partition |= '/internalPartition' in args
                block = [line]
            else:
                block.append(line)
        self.assertTrue(saw_internal_partition,
                        'no pkg-impl.cpp edge carries /internalPartition')
        # Both classes hold the full partition set (the divergent variant
        # recompiled every unit, including the internal partition).
        cpp = self.host_cpp_compiler()
        cache = os.path.join(self.builddir, cpp.get_module_cache_dir())
        suffix = cpp.get_module_bmi_suffix()
        for d in self.bmi_class_dirs():
            for name in ('pkg', 'pkg-part', 'pkg-impl'):
                path = os.path.join(cache, d, name + suffix)
                self.assertTrue(os.path.isfile(path), f'missing BMI {path}')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_modules_pch(self):
        # PCH and modules in one target on cl. A module interface unit gets no
        # PCH (the forced include would land before the module declaration);
        # ordinary TUs keep it. cl's /scanDependencies force-includes the PCH
        # header from disk, so a module target's scan needs the header's dir on
        # the include path -- a non-module target resolves it from the baked
        # .pch (this exercises that fix; the fixture declares no include_dirs).
        self.build_and_check_modules('170 cpp modules pch', bmis=['modlib'])
        # main.cpp (ordinary TU) uses the PCH (/Yu); modlib.cppm (interface)
        # never does. Edge blocks: a build line plus its indented ARGS.
        yu_main = yu_iface = False
        block = []
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            ninja = f.read()
        for line in ninja.splitlines() + ['end']:
            if not line or not line[0].isspace():
                bl = ' '.join(block)
                if any(b.startswith('build ') and 'main.cpp' in b for b in block):
                    yu_main |= '/Yu' in bl
                if any(b.startswith('build ') and 'modlib.cppm' in b for b in block):
                    self.assertNotIn('/Yu', bl)
                block = [line]
            else:
                block.append(line)
        self.assertTrue(yu_main, 'consumer TU should use the PCH (/Yu)')

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='msvc')
    def test_msvc_stl_header_units(self):
        # A real standard-library header as a header unit on cl: <vector>
        # resolves to an absolute system path, built into the ifc cache and
        # named on the consumer's command line (/headerUnit), so the default
        # BMI-on-ARGS check is off. A define-divergent second consumer makes
        # the build multi-class.
        self.build_and_check_modules('171 stl header units',
                                     ninja_args_not_contains=())

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_module_source_with_spaces(self):
        # A module interface source whose file name contains a space: the BMI
        # is named from the module name, but object-derived paths (.ddi and the
        # BMI-only variant the divergent consumer demands) inherit the spaced
        # basename and must survive it.
        self.build_and_check_modules('172 module sources with spaces')
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_runtime_bmi_classes(self):
        # The MSVC runtime library (/MD vs /MT) is BMI-affecting and not
        # allowlisted, so a consumer that overrides b_vscrt lands in its own
        # BMI class: prog_mt resolves modlib against a BMI-only variant compiled
        # /MT and links the provider's runtime-neutral object, while prog_md
        # shares the provider's own /MD BMI. Same two-class contract as a
        # cpp_std split, driven by a runtime flag on real cl.
        self.check_bmi_classes('176 msvc runtime bmi classes', module_name='modlib',
                               provider_lib='libmodlib.a',
                               consumers=('prog_md.exe', 'prog_mt.exe'),
                               expected_targets=('modlib', 'prog_md', 'prog_mt'))

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_module_rtti_divergence_builds(self):
        # cpp_rtti (/GR-) is not on cl's class-key strip list, so prog resolves
        # modlib through a BMI-only variant instead of importing a
        # /GR-mismatched provider BMI; the constexpr probe turns a wrongly
        # shared BMI into a wrong exit code.
        self.build_and_check_modules('177 msvc module rtti divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_module_define_divergence_builds(self):
        # A -D divergence splits the class on cl too: prog resolves modlib
        # through a BMI-only variant built without -DFOO instead of importing
        # a FOO-mismatched provider BMI.
        self.build_and_check_modules('178 msvc module define divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='msvc')
    def test_msvc_header_unit_rtti_divergence_builds(self):
        # Each declarer of the same header unit gets its own unit BMI when
        # cpp_rtti diverges: prog_a and prog_b must not share one.
        self.build_and_check_modules('179 msvc header unit rtti divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_digests('util.h')), 2)

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='msvc')
    def test_msvc_header_unit_define_divergence_builds(self):
        # Same as above, driven by a -D divergence instead of cpp_rtti.
        self.build_and_check_modules('180 msvc header unit define divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_digests('util.h')), 2)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_module_eh_divergence_builds(self):
        # cpp_eh (/EHs-c- vs /EHsc) must split the class: prog resolves
        # modlib through a BMI-only variant instead of importing an
        # /EH-mismatched provider BMI; the constexpr probe turns a wrongly
        # shared BMI into a wrong exit code.
        self.build_and_check_modules('181 msvc module eh divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='msvc')
    def test_msvc_header_unit_eh_divergence_builds(self):
        # Each declarer of the same header unit gets its own unit BMI when
        # cpp_eh diverges: prog_a and prog_b must not share one.
        self.build_and_check_modules('182 msvc header unit eh divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_digests('util.h')), 2)

    def _private_bmi_names(self, private_dir):
        """The bare module names (basename minus BMI suffix) cl actually
        wrote into the given private BMI directory, read from disk -- a flag
        naming the directory does not prove cl wrote there."""
        suffix = self.host_cpp_compiler().get_module_bmi_suffix()
        return {os.path.basename(p)[:-len(suffix)]
                for p in glob(os.path.join(self.builddir, private_dir, f'*{suffix}'))}

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_private_executable_modules(self):
        # Nothing can ever link an executable, so its modules are private to
        # it: two executables exporting the same module name must not
        # collide over the shared ifc.cache directory. cl writes BMIs by
        # /ifcOutput directory, so this must be confirmed on disk, not just
        # from the command line.
        self.build_and_check_modules('185 private executable modules')
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 2)
        for d in dirs:
            self.assertEqual(self._private_bmi_names(d), {'tests'},
                             f'{d}: expected exactly one private tests BMI')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_private_executable_imports_dependency(self):
        # An executable's own private module and a linked library's public
        # module must both resolve in the same target: the private dir and
        # the shared cache both ride /ifcSearchDir on one command line, while
        # /ifcOutput must pick only the private one for this target's own
        # interface.
        self.build_and_check_modules('186 private executable imports dependency',
                                     bmis=['libmod'])
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 1)
        private_dir = next(iter(dirs))
        self.assertEqual(self._private_bmi_names(private_dir), {'tests'})
        # libmod's public module must never leak into the private dir.
        self.assertNotIn('libmod', self._private_bmi_names(private_dir))

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces(self):
        # A library mixing a public interface (api) with two private ones
        # (detail; hidden with its own independently-private internal
        # partition): api's implementation unit imports both its own private
        # modules and a linked dependency's public module in one TU, so that
        # compile carries two /ifcSearchDir entries (private dir, then the
        # shared cache) while /ifcOutput for each interface source must pick
        # the right one of the two, per compile.
        self.build_and_check_modules('187 cpp private module interfaces',
                                     bmis=['api', 'pub'])
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 1)
        private_dir = next(iter(dirs))
        self.assertEqual(self._private_bmi_names(private_dir),
                         {'detail', 'hidden', 'hidden-impl'},
                         'mylib private interfaces did not all land in the private dir')
        # Neither private module ever reaches the shared cache: only the
        # public ones (api, pub) may live there.
        for name in ('detail', 'hidden', 'hidden-impl'):
            self.assertFalse(os.path.isfile(self.bmi_path(name)),
                             f'{name} must not be in the shared BMI cache')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_direct_import(self):
        # A target outside the providing library imports its private module
        # directly: rejected with a diagnostic naming both the module and the
        # target that provides it privately, not the generic "no target"
        # error a reader would otherwise go hunting for a missing dependency
        # over.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=direct-import'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'requires module "detail", which target \'mylib\' provides privately '
            "(it is listed in that target's cpp_private_module_interfaces). A "
            'private module can only be imported inside the target that provides it.',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_missing_not_swallowed(self):
        # A module nowhere in the build still gets the untouched generic
        # error, even in a project that has private modules elsewhere: the
        # private-module branch must not swallow the genuine "missing" case.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=missing'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('provided by no target in this build', cm.exception.stdout)
        self.assertNotIn('provides privately', cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_cycle(self):
        # A dependency cycle entirely among a target's own private modules
        # must still be caught, exactly as for public ones.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=cycle'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('C++ module dependency cycle', cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_both_kwargs_error(self):
        # Listing a source in both cpp_module_interfaces and
        # cpp_private_module_interfaces is a configure-time error: being
        # private already implies being an interface, so the combination is
        # ambiguous rather than meaningful.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        out = self.init(testdir, extra_args=['-Dmode=both-kwargs'], allow_fail=True)
        self.assertIn('in both cpp_module_interfaces and cpp_private_module_interfaces', out)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_name_collision(self):
        # Two libraries each privately export a module literally named
        # "detail", each linked into its own separate executable: privacy
        # removes the whole-build-tree name claim, so this builds even
        # though nothing here would pass the public-module uniqueness check.
        self.build_and_check_modules('189 two libraries private module name collision')
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 2)
        self.assertFalse(os.path.isfile(self.bmi_path('detail')))
        self.assertFalse(os.path.isfile(self.bmi_path('detail') + '.owner'),
                         'a private module must never take a global name claim')
        # Each private dir independently holds its own same-named detail BMI.
        for d in dirs:
            self.assertEqual(self._private_bmi_names(d), {'detail'},
                             f'{d}: expected its own private detail BMI')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_name_collision_in_one_link(self):
        # Both libraries' private "detail" modules reaching the SAME link is
        # still a hard error, and must stay one: a module's exported entities
        # are mangled from its bare module name alone, so two same-named
        # private modules linked together would silently collide at the
        # symbol level with no link error at all. Privacy must not remove
        # this check, only the whole-build-tree one.
        testdir = os.path.join(self.unit_test_dir, '189 two libraries private module name collision')
        self.init(testdir, extra_args=['-Dmode=collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided by more than one target reaching this link',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_same_name_targets(self):
        # Two static_library('util') targets in different subdirs -- a target
        # name is only unique within its subdir -- each privately exporting
        # "detail", each linked into its own executable. Each private ifc dir
        # must independently hold its own same-named detail BMI: cl writes
        # BMIs by /ifcOutput directory, so only disk shows it.
        self.build_and_check_modules('193 same name targets private module collision')
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 2)
        self.assertFalse(os.path.isfile(self.bmi_path('detail')))
        self.assertFalse(os.path.isfile(self.bmi_path('detail') + '.owner'),
                         'a private module must never take a global name claim')
        for d in dirs:
            self.assertEqual(self._private_bmi_names(d), {'detail'},
                             f'{d}: expected its own private detail BMI')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_same_name_targets_in_one_link(self):
        # The same collision as above, between two targets that share a name:
        # the check must key on target identity, not on the name, or the two
        # providers read as one and the ODR violation ships silently. The
        # diagnostic must also tell two same-named targets apart.
        testdir = os.path.join(self.unit_test_dir, '193 same name targets private module collision')
        self.init(testdir, extra_args=['-Dmode=collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided by more than one target reaching this link '
            "('util' (defined in sub1/meson.build) and 'util' (defined in sub2/meson.build))",
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_distinct_providers_in_one_link(self):
        # Two private-module providers in one link whose module names differ:
        # only the module name is ever mangled into a symbol, so this is no
        # collision at all and must keep building.
        self.build_and_check_modules('193 same name targets private module collision',
                                     extra_args=['-Dmode=distinct'])

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_cpp_private_module_own_and_dep_collision(self):
        # A target's own private module colliding with the private module of a
        # library it links is the same symbol-level collision as two libraries
        # colliding with each other -- and it is invisible to the dep-vs-dep
        # check, since a target is never among its own dependencies.
        testdir = os.path.join(self.unit_test_dir, '193 same name targets private module collision')
        self.init(testdir, extra_args=['-Dmode=own-collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided both by this target (\'prog\') and by '
            "'util' (defined in sub1/meson.build), which it links",
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_cpp_private_module_interfaces_variant_exclusion(self):
        # A private module (priv) alongside a public one (pubmod) that
        # crosses a BMI class boundary: the synthesized BMI-only variant must
        # recompile only the public interface, never the private one. MSVC
        # has no cpp_std=c++23, so the divergent consumer is driven at
        # c++latest instead (see the fixture's divergent_std option).
        self.build_and_check_modules('190 private module bmi class exclusion',
                                     extra_args=['-Ddivergent_std=c++latest'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        variant_lines = [line for line in contents.splitlines() if '@bmi@' in line]
        self.assertTrue(variant_lines)
        # 'priv' alone would also match the unrelated 'meson-private' prefix
        # every private BMI dir (and every variant's own private_dir) uses;
        # the private *interface* is named 'priv.cppm' (source) or
        # 'priv.cpp.obj' (its output).
        self.assertFalse(any('priv.' in line for line in variant_lines),
                         'a BMI-only variant must never compile a private interface')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_export_dynamic_executable_private_modules(self):
        # An export_dynamic executable can be linked -- on PE/COFF through the
        # import library that same kwarg produces -- so it is a normal module
        # provider: its declared private interface stays in its private
        # /ifcOutput dir while its public one must reach the shared ifc cache
        # for the shared_module linking it. cl writes BMIs by /ifcOutput
        # directory, so confirm the split on disk, not from the command line.
        self.build_and_check_modules('192 export dynamic private modules', bmis=['pub'])
        self.assertEqual(self.provided_modules('app'), {'pub'})
        dirs = self.private_bmi_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertEqual(self._private_bmi_names(next(iter(dirs))), {'priv'},
                         "the executable's public module must not land in its private dir")
        self.assertFalse(os.path.isfile(self.bmi_path('priv')),
                         'a private module must not reach the shared BMI cache')

    @requires_cpp_module_caps('modules', compiler='msvc')
    def test_msvc_export_dynamic_executable_private_module_import(self):
        # The plugin imports the executable's private module instead of its
        # public one: privacy is not weakened by the target being linkable.
        testdir = os.path.join(self.unit_test_dir, '192 export dynamic private modules')
        self.init(testdir, extra_args=['-Dmode=import-private'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'requires module "priv", which target \'app\' provides privately '
            "(it is listed in that target's cpp_private_module_interfaces). A "
            'private module can only be imported inside the target that provides it.',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='msvc')
    def test_msvc_export_dynamic_executable_variant_provider(self):
        # The plugin diverges on cpp_std, so the executable's public module is
        # recompiled into a BMI-only variant for the plugin's class -- an
        # executable as a variant provider, which only a linkable one can be.
        # cl has no cpp_std=c++23, so the divergent dialect is c++latest here.
        self.build_and_check_modules('192 export dynamic private modules',
                                     extra_args=['-Dmode=divergent',
                                                 '-Ddivergent_std=c++latest'])
        self.assertEqual(len(self.bmi_class_dirs()), 2)
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        self.assertEqual(self.provided_modules('app'), {'pub'})
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            variant_lines = [line for line in f.read().splitlines() if '@bmi@' in line]
        self.assertTrue(variant_lines)
        # 'priv' alone would also match the unrelated 'meson-private' prefix of
        # every private BMI dir; the private interface is named 'priv.cppm'
        # (source) or 'priv.ifc'/'priv.cpp.obj' (its outputs).
        self.assertFalse(any('priv.' in line for line in variant_lines),
                         'a BMI-only variant must never compile a private interface')

    def test_gcc_header_unit_space_free_alias(self):
        # The space-free alias that lets a GCC header unit under a spaced path
        # be named in a module mapper (a mapper key cannot contain whitespace):
        # on Windows a directory symlink where the build is privileged, else an
        # NTFS junction. Either must be traversable and idempotent. This is pure
        # path machinery, independent of any compiler.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        import mesonbuild.backend.ninjabackend as nb
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, 'build')
            real = os.path.join(d, 'sp ace src')
            os.makedirs(build)
            os.makedirs(real)
            with open(os.path.join(real, 'hdr.h'), 'w', encoding='utf-8') as f:
                f.write('#pragma once\n')
            be = NinjaBackend.__new__(NinjaBackend)
            be._dir_aliases = {}
            be.environment = mock.MagicMock()
            be.environment.get_build_dir.return_value = build

            rel = be._space_free_dir_alias(real)
            if rel is None:
                raise SkipTest('platform can make neither a symlink nor a junction here')
            self.assertRegex(rel, r'^meson-private/imap/[0-9a-f]+$')
            abs_alias = os.path.join(build, rel)
            # Traversable: the aliased path reaches the real file.
            with open(os.path.join(abs_alias, 'hdr.h'), encoding='utf-8') as f:
                self.assertIn('pragma once', f.read())
            # It is a link/junction, not a copied tree.
            self.assertIsNotNone(be._read_dir_link(abs_alias))
            # Idempotent across a fresh backend, exercising the on-disk readback
            # (a junction's readlink carries a '\\?\' prefix that must compare
            # equal to the plain target, so it is not recreated every time).
            be._dir_aliases = {}
            self.assertEqual(be._space_free_dir_alias(real), rel)

            # Force the junction fallback: an unprivileged build cannot make a
            # symlink, but a junction still names the unit on local NTFS.
            real2 = os.path.join(d, 'sp ace two')
            os.makedirs(real2)
            with open(os.path.join(real2, 'hdr.h'), 'w', encoding='utf-8') as f:
                f.write('#pragma once\n')
            be._dir_aliases = {}
            with mock.patch.object(nb.os, 'symlink', side_effect=OSError):
                rel2 = be._space_free_dir_alias(real2)
            if rel2 is not None:
                with open(os.path.join(build, rel2, 'hdr.h'), encoding='utf-8') as f:
                    self.assertIn('pragma once', f.read())

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_units(self):
        # A user header unit, a system header unit and a named module in one
        # target build, link and run on Windows. This fixture's own path
        # contains spaces, which a mapper key cannot hold, so each unit is named
        # through the space-free alias, and the BMI the mapper points at is the
        # one the unit edge wrote.
        self.build_and_check_modules('142 gcc header units',
                                     setup_not_contains=['divergent dialects',
                                                         'cannot name a header unit'])
        self.check_gcc_module_mappers()
        import glob
        mappers = glob.glob(os.path.join(self.builddir, '*', 'main.cpp.*.mapper'))
        self.assertEqual(len(mappers), 1, f'expected one main.cpp mapper, got {mappers}')
        with open(mappers[0], encoding='utf-8') as f:
            mapper = f.read().splitlines()
        for unit in ('util.h', 'angleutil.h'):
            pat = rf'\./meson-private/imap/[0-9a-f]+/{re.escape(unit)} \S+\.gcm'
            line = next((l for l in mapper if re.fullmatch(pat, l)), None)
            self.assertIsNotNone(line, f'no aliased mapping for {unit} in {mapper}')
            bmi = line.split(' ', 1)[1]
            self.assertTrue(os.path.isfile(os.path.join(self.builddir, bmi)),
                            f'mapper names {bmi}, which the unit edge did not write')

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_alias_unavailable_warns(self):
        # Where no space-free alias can be made (a non-NTFS build volume, or a
        # build without symlink privilege or junction support), a header unit
        # under a spaced path stays unnameable and Meson says so at configure
        # rather than emitting a mapper that fails at compile. Block the alias
        # directory with a regular file to force that platform's behavior.
        os.makedirs(os.path.join(self.builddir, 'meson-private'), exist_ok=True)
        with open(os.path.join(self.builddir, 'meson-private', 'imap'), 'w',
                  encoding='utf-8') as f:
            f.write('')
        out = self.init(os.path.join(self.unit_test_dir, '142 gcc header units'))
        self.assertIn('cannot name a header unit whose path contains a space', out)
        # All or nothing: with no alias, nothing is respelled -- a half-aliased
        # target would name the unit one way on its edge and another on its
        # importers, worse than the diagnosed failure.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('meson-private/imap/', f.read())

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_aliasing(self):
        # Windows counterpart of LinuxlikeTests.test_header_unit_aliasing: the
        # alias link edge (copy.py --link, ninja rule cpp_header_unit_alias) and
        # the grouping it depends on have never run on this platform.
        testdir = '173 header unit aliasing'
        self.build_and_check_modules(testdir, ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_bmis('header.hpp')), 1)

        self.new_builddir()
        self.build_and_check_modules(testdir, extra_args=['-Dmode=aliased'],
                                     ninja_args_not_contains=(),
                                     setup_not_contains=['cannot name a header unit'])
        self.assertEqual(len(self.header_unit_bmis('header.hpp')), 1)
        self.check_gcc_module_mappers()
        alias_bmi = self.assert_alias_mapper_key('prog', 'foo_aliased.cpp')
        plain_bmi = self.assert_alias_mapper_key('prog', 'main.cpp', alias=False)
        self.assertEqual(alias_bmi, plain_bmi,
                         'both spellings must resolve the one BMI of the file')
        # The alias's default-named path holds the canonical BMI itself (a hard
        # link on NTFS, copy.py's fallback elsewhere) -- not a second build of
        # it -- and a follow-up build must see nothing to do.
        self.assert_header_unit_alias_link(alias_bmi)
        self.assertBuildIsNoop()

        self.new_builddir()
        self.build_and_check_modules(testdir, extra_args=['-Dmode=classes'],
                                     ninja_args_not_contains=(),
                                     setup_not_contains=['cannot name a header unit'])
        units = self.header_unit_bmis('header.hpp')
        self.assertEqual(len(units), 2, f'expected one BMI per class, got {units}')
        per_prog = {}
        for prog in ('prog', 'progfoo'):
            alias_bmi = self.assert_alias_mapper_key(prog, 'foo_aliased.cpp')
            plain_bmi = self.assert_alias_mapper_key(prog, 'main.cpp', alias=False)
            self.assertEqual(alias_bmi, plain_bmi,
                             f'{prog} must reach one BMI through either spelling')
            per_prog[prog] = alias_bmi
        self.assertNotEqual(per_prog['prog'], per_prog['progfoo'],
                            'divergent classes must not share a unit BMI')
        self.assert_header_unit_alias_link(per_prog['prog'])
        self.assertBuildIsNoop()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_gcc_stl_header_units(self):
        # Windows counterpart of LinuxlikeTests.test_gcc_stl_header_units:
        # <vector> resolves to a drive-letter absolute path here, and
        # flat_cmi_path must mangle its colon (a literal one reads as an NTFS
        # alternate-data-stream separator) the same way it mangles '.' and
        # '..' -- nothing on Windows exercised that path before.
        self.build_and_check_modules('171 stl header units',
                                     setup_not_contains=['cannot name a header unit'])
        self.check_gcc_module_mappers()
        import glob
        mappers = glob.glob(os.path.join(self.builddir, 'prog.*.p', 'main.cpp.*.mapper'))
        self.assertEqual(len(mappers), 1, f'expected one main.cpp mapper, got {mappers}')
        with open(mappers[0], encoding='utf-8') as f:
            mapper = f.read().splitlines()
        for line in mapper:
            key, _, bmi = line.partition(' ')
            if os.path.basename(key) == 'vector':
                self.assertRegex(key, r'^[A-Za-z]:/\S+/vector$')
                want = f'gcm.cache/{key[0]}-{key[2:]}.gcm'
                self.assertEqual(bmi.replace('\\', '/'), want.replace('\\', '/'))
                break
        else:
            self.fail(f'no vector header-unit mapping in {mapper}')

    def test_non_utf8_fails(self):
        # FIXME: VS backend does not use flags from compiler.get_always_args()
        # and thus it's missing /utf-8 argument. Was that intentional? This needs
        # to be revisited.
        if self.backend is not Backend.ninja:
            raise SkipTest(f'This test only pass with ninja backend (not {self.backend.name}).')
        testdir = os.path.join(self.platform_test_dir, '18 msvc charset')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_argument_syntax() != 'msvc':
            raise SkipTest('Not using MSVC')
        self.init(testdir, extra_args=['-Dtest-failure=true'])
        self.assertRaises(subprocess.CalledProcessError, self.build)

    @unittest.skipIf(is_cygwin(), "Needs visual studio")
    def test_vsenv_option(self):
        if self.backend is not Backend.ninja:
            raise SkipTest('Only ninja backend is valid for test')
        env = os.environ.copy()
        env['MESON_FORCE_VSENV_FOR_UNITTEST'] = '1'
        # Remove ninja from PATH to ensure that the one provided by Visual
        # Studio is picked, as a regression test for
        # https://github.com/mesonbuild/meson/issues/9774
        env['PATH'] = get_path_without_cmd('ninja', env['PATH'])
        # Add a multiline variable to test that it is handled correctly
        # with a line that contains only '=' and a line that would result
        # in an invalid variable name.
        # see: https://github.com/mesonbuild/meson/pull/13682
        env['MULTILINE_VAR_WITH_EQUALS'] = 'Foo\r\n=====\r\n'
        env['MULTILINE_VAR_WITH_INVALID_NAME'] = 'Foo\n%=Bar\n'
        testdir = os.path.join(self.common_test_dir, '1 trivial')
        out = self.init(testdir, extra_args=['--vsenv'], override_envvars=env)
        self.assertIn('Activating VS', out)
        self.assertRegex(out, 'Visual Studio environment is needed to run Ninja')
        # All these directly call ninja with the full path, so we need to patch
        # it out to use meson subcommands
        with mock.patch.object(self, 'build_command', self.meson_command + ['compile']):
            out = self.build(override_envvars=env)
            self.assertIn('Activating VS', out)
        with mock.patch.object(self, 'test_command', self.meson_command + ['test']):
            out = self.run_tests(override_envvars=env)
            self.assertIn('Activating VS', out)
        with mock.patch.object(self, 'install_command', self.meson_command + ['install']):
            out = self.install(override_envvars=env)
            self.assertIn('Activating VS', out)
