# SPDX-License-Identifier: Apache-2.0
# Copyright © 2021-2024 Intel Corporation

"""Accumulator for p1689r5 module dependencies.

See: https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2022/p1689r5.html
"""

from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import textwrap
import typing as T

from ..utils.core import MesonException

if T.TYPE_CHECKING:
    from .depscan import Description, Require, Rule

# The quoting logic has been copied from the ninjabackend to avoid having to
# import half of Meson just to quote outputs, which is a performance problem
_QUOTE_PAT = re.compile(r'[$ :\n]')


def quote(text: str) -> str:
    # Fast path for when no quoting is necessary
    if not _QUOTE_PAT.search(text):
        return text
    if '\n' in text:
        errmsg = textwrap.dedent(f'''\
            Ninja does not support newlines in rules. The content was:

            {text}

            Please report this error with a test case to the Meson bug tracker.''')
        raise RuntimeError(errmsg)
    return _QUOTE_PAT.sub(r'$\g<0>', text)


_PROVIDER_CACHE: T.Dict[str, str] = {}


def get_provider(rules: T.List[Rule], name: str) -> T.Optional[str]:
    """Get the object that a module from another Target provides

    We must rely on the object file here instead of the module itself, because
    the object rule is part of the generated build.ninja, while the module is
    only declared inside a dyndep. This creates for the dyndep generator to
    depend on previous dyndeps as order deps. Since the module
    interface file will be generated when the object is generated we can rely on
    that in proxy and simplify generation.

    :param rules: The list of rules to check
    :param name: The logical-name to look for
    :raises RuntimeError: If no provider can be found
    :return: The object file of the rule providing the module
    """
    # Cache the result for performance reasons
    if name in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[name]

    for r in rules:
        for p in r.get('provides', []):
            if p['logical-name'] == name:
                obj = r['primary-output']
                _PROVIDER_CACHE[name] = obj
                return obj
    return None


def process_rules(rules: T.List[Rule],
                  extra_rules: T.List[Rule],
                  ) -> T.Iterable[T.Tuple[str, T.Optional[T.List[str]], T.List[str]]]:
    """Process the rules for this Target

    :param rules: the rules for this target
    :param extra_rules: the rules for all of the targets this one links with, to use their provides
    :yield: A tuple of the output, the exported modules, and the consumed modules
    """
    for rule in rules:
        prov: T.Optional[T.List[str]] = None
        req: T.List[str] = []
        if 'provides' in rule:
            prov = [p['compiled-module-path'] for p in rule['provides']]
        if 'requires' in rule:
            for p in rule['requires']:
                modfile = p.get('compiled-module-path')
                if modfile is not None:
                    req.append(modfile)
                else:
                    # We can't error if this is not found because of compiler
                    # provided modules
                    found = get_provider(extra_rules, p['logical-name'])
                    if found:
                        req.append(found)
        yield rule['primary-output'], prov, req


def formatter(files: T.Optional[T.List[str]]) -> str:
    if files:
        fmt = ' '.join(quote(f) for f in files)
        return f'| {fmt}'
    return ''


def gen(outfile: str, desc: Description, extra_rules: T.List[Rule]) -> int:
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write('ninja_dyndep_version = 1\n\n')

        for obj, provides, requires in process_rules(desc['rules'], extra_rules):
            ins = formatter(requires)
            out = formatter(provides)
            f.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')

    return 0


def module_to_filename(name: str, bmidir: str, suffix: str) -> str:
    """Map a C++ module logical-name to its BMI path.

    The compiler names a module's BMI <bmidir>/<name><suffix> with a partition
    separator ':' becoming '-' (gcm.cache/pkg-part.gcm for GCC,
    ifc.cache/pkg-part.ifc for MSVC). Mirrors the compiler's
    module_name_to_filename; kept here so the collator names BMIs from
    logical-names alone -- the P1689 output does not carry a
    compiled-module-path. bmidir/suffix are passed by the backend from the
    compiler so the two stay in lockstep.
    """
    return f'{bmidir}/{name.replace(":", "-")}{suffix}'


def _flat_cmi_path(logical_name: str, flat_dir: str, suffix: str) -> str:
    """The CMI path GCC's default (mapper-less) mapping gives a header unit.

    A header unit's logical-name is its resolved header path; GCC stores its
    CMI under the flat cache root with '.' and '..' components mangled to ','
    and ',,' and an absolute path appended as-is: './util.h' ->
    'gcm.cache/,/util.h.gcm', './../srcx/hdr.h' ->
    'gcm.cache/,/,,/srcx/hdr.h.gcm', '/usr/include/c++/16/vector' ->
    'gcm.cache/usr/include/c++/16/vector.gcm'. A per-TU mapper disables the
    default mapping entirely, so it must reproduce this scheme for the units
    the TU imports: GCC header units stay in the flat shared cache, which no
    per-class relocation flag can move.
    """
    parts = [',' * len(p) if p in ('.', '..') else p
             for p in logical_name.split('/')]
    return f'{flat_dir}/' + '/'.join(p for p in parts if p) + suffix


def _write_if_different(path: str, content: str) -> None:
    """Write only when the content changed, preserving mtime otherwise.

    Mapper files are implicit inputs of compile edges; an unconditional
    rewrite would recompile every object in the target whenever any module
    in it changes.
    """
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == content:
                return
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _is_header_unit(req: Require) -> bool:
    # A header-unit require is skipped by the collator: its BMI is pre-built by a
    # static edge, so it is neither a dyndep dependency nor a missing named
    # module. cl tags it with a lookup-method of include-quote/include-angle (a
    # named module is by-name). GCC omits lookup-method, so fall back to shape:
    # a header unit's logical-name is a resolved header path ('./util.h',
    # '/usr/.../vector', or on Windows '.\\util.h' / 'C:\\...'), which always
    # contains a path separator; a named module or ':partition' never does.
    method = req.get('lookup-method')
    if method in ('include-quote', 'include-angle'):
        return True
    if method == 'by-name':
        return False
    name = req['logical-name']
    return '/' in name or '\\' in name


def _check_module_cycle(rules: T.List[Rule], provided: T.Dict[str, str]) -> None:
    """Raise on a dependency cycle among this target's own modules.

    Nodes are the module names provided in this target; an edge goes from a
    provided module to each module (also provided here) that its translation
    unit requires. A back-edge in a DFS is a cycle; report it before ninja's own
    generic cycle detector would.
    """
    deps: T.Dict[str, T.List[str]] = {}
    for rule in rules:
        local_reqs = [r['logical-name'] for r in rule.get('requires', [])
                      if r['logical-name'] in provided]
        for prov in rule.get('provides', []):
            deps.setdefault(prov['logical-name'], []).extend(local_reqs)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: T.Dict[str, int] = {n: WHITE for n in deps}
    path: T.List[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for nxt in deps.get(node, []):
            if color.get(nxt, BLACK) == GRAY:
                cycle = path[path.index(nxt):] + [nxt]
                raise MesonException(
                    'C++ module dependency cycle: ' + ' -> '.join(cycle))
            if color.get(nxt, BLACK) == WHITE:
                visit(nxt)
        path.pop()
        color[node] = BLACK

    for name in deps:
        if color[name] == WHITE:
            visit(name)


def _claim_module_provider(name: str, cache_bmi: str, provmap: str) -> None:
    """Enforce one providing target per module name per build tree.

    Unrelated targets never meet in a collate (--dep-provmap carries only
    linked dependencies), but every provider's BMI lands in the shared module
    cache at a path keyed by the module name alone, so two exporters of one
    name would silently fight over the same BMI file and wedge the build.
    Record the owning target's provmap path next to the would-be BMI; a second
    claimant errors. A claim is stale -- and taken over -- when its provmap is
    gone (target removed; meson reconfigure runs `ninja -t cleandead`) or no
    longer lists the module (the module moved and the old provider
    re-collated).
    """
    owner_file = cache_bmi + '.owner'
    os.makedirs(os.path.dirname(owner_file), exist_ok=True)
    try:
        fd = os.open(owner_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        with open(owner_file, encoding='utf-8') as f:
            owner = f.read()
        if owner == provmap:
            return
        live = False
        if os.path.exists(owner):
            try:
                with open(owner, encoding='utf-8') as f:
                    live = name in json.load(f)
            except (OSError, ValueError):
                pass
        if live:
            raise MesonException(
                f'Module "{name}" is exported by more than one target in this '
                f'build ({os.path.dirname(owner)} and {os.path.dirname(provmap)}); '
                f'both would write their BMI to {cache_bmi}. A module name may '
                'have only one providing target per build tree. (If the module '
                'recently moved between targets this claim may be stale; re-run '
                'ninja once.)')
        with open(owner_file, 'w', encoding='utf-8') as f:
            f.write(provmap)
        return
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(provmap)


def run_p1689(argv: T.List[str]) -> int:
    """Collate P1689 scans into a dyndep + a provided-module map.

    Consumes this target's per-source .ddi files and the provided-module maps of
    its dependency targets, emitting a Ninja dyndep that orders each object
    against the BMIs it requires/provides, plus this target's own map. The BMI
    directory and suffix (--bmi-dir/--bmi-suffix) come from the compiler so the
    logical-name -> BMI mapping matches the compiler's own.

    With --mapper-suffix it also writes a GCC module mapper per translation
    unit (<primary-output><suffix>, an implicit input of the compile edge)
    naming the TU's provides and direct imports: GCC has no per-class
    module-search-path flag, so relocating BMIs into a class subdirectory
    means enumerating them per TU. Transitive imports need no entries (GCC
    reads them from the direct imports' CMIs), so the file is linear in the
    TU's imports. Written copy-if-different: an unconditional rewrite would
    recompile the whole target whenever any module in it changed.
    """
    parser = argparse.ArgumentParser(prog='depaccumulate --p1689')
    parser.add_argument('--dyndep', required=True, help='Output Ninja dyndep file.')
    parser.add_argument('--provmap', required=True,
                        help="Output provided-module map for this target.")
    parser.add_argument('--bmi-dir', required=True,
                        help='Directory the compiler names BMIs in (e.g. gcm.cache).')
    parser.add_argument('--bmi-suffix', required=True,
                        help='BMI file suffix including the dot (e.g. .gcm).')
    parser.add_argument('--dep-provmap', action='append', default=[],
                        help='Provided-module map of a dependency target. Repeatable.')
    parser.add_argument('--stamp-suffix', default=None,
                        help='BMIs reach the shared cache via harvest edges (Clang\'s '
                             'own pipeline, and BMI-only variants on any compiler): '
                             'map a provided module to its harvest stamp '
                             '(<primary-output> + this suffix) instead of the cache BMI '
                             'path, and declare no implicit outputs on object edges.')
    parser.add_argument('--interface-source', action='append', default=[],
                        dest='interface_sources',
                        help='A source compiled as an interface unit despite lacking a '
                             'module extension (declared via cpp_module_interfaces, or '
                             'any recorded interface of a BMI-only variant). Its '
                             'provides pass the interface-extension check. Repeatable.')
    parser.add_argument('--header-unit', action='append', default=[], dest='header_units',
                        help='A declared header unit as "<mode>:<spelling>". Repeatable.')
    parser.add_argument('--mapper-suffix', default=None,
                        help='Also write a GCC module mapper per TU at '
                             '<primary-output> + this suffix, mapping its provides '
                             'and direct imports to their BMI paths.')
    parser.add_argument('--flat-bmi-dir', default=None,
                        help='The unkeyed shared cache dir (e.g. gcm.cache) header-unit '
                             'imports resolve in; mappers reproduce the compiler\'s '
                             'default header-unit CMI naming under it.')
    parser.add_argument('ddis', nargs='*', help="This target's P1689 scan results.")
    args = parser.parse_args(argv)

    # (mode, spelling) of every header unit declared on this target, so a source
    # that imports an undeclared one can be flagged. Only cl reports header-unit
    # requires from a cold scan (with a lookup-method), so this check is
    # effective for MSVC; GCC fails earlier, at the scan itself.
    declared_units = {tuple(hu.split(':', 1)) for hu in args.header_units}
    interface_sources = {os.path.normpath(p) for p in args.interface_sources}

    rules: T.List[Rule] = []
    for ddi in args.ddis:
        with open(ddi, encoding='utf-8') as f:
            data: Description = json.load(f)
        rules.extend(data.get('rules', []))

    # name -> BMI path for everything resolvable here (local + linked deps).
    resolvable: T.Dict[str, str] = {}
    # name -> BMI path for what this target provides (the map we publish).
    provided: T.Dict[str, str] = {}
    # name -> human-readable provider (object file or dep-map path), used for
    # duplicate diagnostics.
    provider_of: T.Dict[str, str] = {}
    for rule in rules:
        obj = rule['primary-output']
        for prov in rule.get('provides', []):
            name = prov['logical-name']
            # A module name may be provided only once within a target.
            if name in provided:
                raise MesonException(
                    f'Module "{name}" is provided by two sources in this target '
                    f'({provider_of[name]} and {obj}). Module names must be unique.')
            if args.stamp_suffix is not None:
                # A harvest edge (and thus the stamp) exists only for sources
                # the backend compiled as interface units, decided by extension
                # alone (in lockstep with generate_single_compile's clang_miu).
                # A provider outside that set would advertise a stamp nothing
                # produces, and consumers would only fail later with the
                # compiler's "module not found" -- reject it here instead.
                src = prov.get('source-path')
                if src is not None \
                        and os.path.splitext(src)[1][1:].lower() not in {'cppm', 'ixx'} \
                        and os.path.normpath(src) not in interface_sources:
                    raise MesonException(
                        f'{src} provides the C++ module "{name}" but is not marked a '
                        'module interface: Clang only emits a module BMI for a source '
                        'compiled as an interface unit, which it must be told per '
                        'source (-x c++-module), and Meson derives that from the '
                        '.cppm/.ixx extension alone. Rename the interface to .cppm or '
                        '.ixx, or list it in the target\'s cpp_module_interfaces '
                        '(cpp_modules: true only covers consumers).')
                # Consumers order against the provider's harvest stamp; the
                # cache BMI itself stays out of Ninja's graph (its name is
                # only known at build time).
                modfile = obj + args.stamp_suffix
            else:
                modfile = module_to_filename(name, args.bmi_dir, args.bmi_suffix)
            provided[name] = modfile
            resolvable[name] = modfile
            provider_of[name] = obj
    for pmfile in args.dep_provmap:
        with open(pmfile, encoding='utf-8') as f:
            imported: T.Dict[str, str] = json.load(f)
        for name, modfile in imported.items():
            # Two targets providing the same module name into one link is
            # IFNDR in GCC (the name is the linkage discriminator).
            if name in resolvable:
                raise MesonException(
                    f'Module "{name}" is provided by more than one target reaching '
                    f'this link ({provider_of[name]} and {pmfile}). Module names '
                    f'must be globally unique within a linked executable.')
            resolvable[name] = modfile
            provider_of[name] = pmfile

    # A module dependency cycle must be reported here rather than left to
    # ninja. Cycles can only occur among modules provided within this target --
    # the target link graph is a DAG -- so the local provides/requires subgraph
    # is enough.
    _check_module_cycle(rules, provided)

    with open(args.dyndep, 'w', encoding='utf-8') as dd:
        dd.write('ninja_dyndep_version = 1\n\n')
        for rule in rules:
            obj = rule['primary-output']
            maplines: T.List[str] = []
            outs: T.List[str] = []
            for prov in rule.get('provides', []):
                name = prov['logical-name']
                if args.stamp_suffix is not None:
                    # The object edge's BMI side-output is declared statically
                    # by the backend; the cache copy belongs to the harvest
                    # edge. The mapper (a BMI-only variant here: GCC is the
                    # only compiler taking both flags) sends the export to
                    # that declared output, the rule's primary-output.
                    maplines.append(f'{name} {obj}')
                else:
                    bmi = module_to_filename(name, args.bmi_dir, args.bmi_suffix)
                    outs.append(bmi)
                    maplines.append(f'{name} {bmi}')
            reqs: T.List[str] = []
            for req in rule.get('requires', []):
                if _is_header_unit(req):
                    # A header unit: pre-built and ordered by static edges, so it
                    # is neither a dyndep dependency nor a missing named module.
                    # cl tags it with a lookup-method and reports it cold, so we
                    # can check here that the source actually declared it.
                    method = req.get('lookup-method')
                    if method in ('include-quote', 'include-angle'):
                        mode = 'user' if method == 'include-quote' else 'system'
                        if (mode, req['logical-name']) not in declared_units:
                            raise MesonException(
                                f'{obj} imports header unit "{req["logical-name"]}", '
                                "which is not declared in this target's "
                                'cpp_header_units.')
                    if args.flat_bmi_dir is not None:
                        maplines.append(f'{req["logical-name"]} ' + _flat_cmi_path(
                            req['logical-name'], args.flat_bmi_dir, args.bmi_suffix))
                    continue
                name = req['logical-name']
                modfile = resolvable.get(name)
                # A required module provided by nothing in the build is an
                # error naming the requiring TU and the missing module.
                if modfile is None:
                    if name in {'std', 'std.compat'}:
                        hint = " (add dependency('std') to this target)"
                    else:
                        hint = (" (if a linked library exports it, build that "
                                'library with cpp_modules: true)')
                    raise MesonException(
                        f'{obj} requires module "{name}", which is provided by no '
                        f'target in this build.{hint}')
                # The compiler must be pointed at the class-cache BMI itself;
                # `modfile` is the ordering handle, which under --stamp-suffix
                # is a harvest stamp rather than a readable BMI.
                maplines.append(f'{name} {module_to_filename(name, args.bmi_dir, args.bmi_suffix)}')
                reqs.append(modfile)
            out = formatter(outs)
            ins = formatter(reqs)
            dd.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')
            if args.mapper_suffix is not None:
                _write_if_different(obj + args.mapper_suffix,
                                    ''.join(line + '\n' for line in maplines))

    with open(args.provmap, 'w', encoding='utf-8') as pm:
        json.dump(provided, pm)

    # Claim the provided names only after publishing the map, so a concurrent
    # collate that loses the claim race always finds a live claimant.
    for name in provided:
        _claim_module_provider(
            name, module_to_filename(name, args.bmi_dir, args.bmi_suffix), args.provmap)

    return 0


def run_harvest(argv: T.List[str]) -> int:
    """Publish a module interface's source-keyed BMI into the shared cache.

    Copies a BMI the compile wrote at a source-keyed path (Clang's bare
    -fmodule-output, or a BMI-only variant's declared output on any compiler)
    to <bmi-dir>/<module-name><bmi-suffix> -- the path consumers' directory
    lookup finds -- with the module name read from the interface's P1689 scan
    at build time, so it never appears on a command line. The stamp is the
    edge's declared output; consumers' dyndep entries order against it
    (run_p1689 --stamp-suffix).
    """
    parser = argparse.ArgumentParser(prog='depaccumulate --harvest')
    parser.add_argument('--pcm', required=True,
                        help='The source-keyed BMI the compile wrote next to the object.')
    parser.add_argument('--ddi', required=True,
                        help="The interface's P1689 scan result.")
    parser.add_argument('--bmi-dir', required=True,
                        help='The shared module cache directory (e.g. pcm.cache).')
    parser.add_argument('--bmi-suffix', required=True,
                        help='BMI file suffix including the dot (e.g. .pcm).')
    parser.add_argument('--stamp', required=True, help='Stamp file to write on success.')
    args = parser.parse_args(argv)

    with open(args.ddi, encoding='utf-8') as f:
        data: Description = json.load(f)
    provides = [p['logical-name']
                for rule in data.get('rules', [])
                for p in rule.get('provides', [])]
    if len(provides) != 1:
        if not provides:
            raise MesonException(
                f'{args.pcm}: its scan ({args.ddi}) reports no provided module; a '
                'module-interface source must contain an export module declaration.')
        raise MesonException(
            f'{args.pcm}: its scan ({args.ddi}) reports more than one provided module '
            f'({", ".join(provides)}); cannot determine the BMI name.')
    os.makedirs(args.bmi_dir, exist_ok=True)
    shutil.copy2(args.pcm, module_to_filename(provides[0], args.bmi_dir, args.bmi_suffix))
    with open(args.stamp, 'w', encoding='utf-8'):
        pass
    return 0


def run(args: T.List[str]) -> int:
    if args and args[0] == '--p1689':
        return run_p1689(args[1:])
    if args and args[0] == '--harvest':
        return run_harvest(args[1:])

    assert len(args) >= 2, 'got wrong number of arguments!'
    outfile, jsonfile, *jsondeps = args
    with open(jsonfile, 'r', encoding='utf-8') as f:
        desc: Description = json.load(f)

    # All rules, necessary for fulfilling across TU and target boundaries
    rules = desc['rules'].copy()
    for dep in jsondeps:
        with open(dep, encoding='utf-8') as f:
            d: Description = json.load(f)
            rules.extend(d['rules'])

    return gen(outfile, desc, rules)
