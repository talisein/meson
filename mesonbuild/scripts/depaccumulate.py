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
import textwrap
import typing as T

from ..utils.core import MesonException

if T.TYPE_CHECKING:
    from .depscan import Description, Rule

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
    parser.add_argument('ddis', nargs='*', help="This target's P1689 scan results.")
    args = parser.parse_args(argv)

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
            outs = [module_to_filename(p['logical-name'], args.bmi_dir, args.bmi_suffix)
                    for p in rule.get('provides', [])]
            reqs: T.List[str] = []
            for req in rule.get('requires', []):
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
                reqs.append(modfile)
            out = formatter(outs)
            ins = formatter(reqs)
            dd.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')

    with open(args.provmap, 'w', encoding='utf-8') as pm:
        json.dump(provided, pm)

    # Claim the provided names only after publishing the map, so a concurrent
    # collate that loses the claim race always finds a live claimant.
    for name in provided:
        _claim_module_provider(
            name, module_to_filename(name, args.bmi_dir, args.bmi_suffix), args.provmap)

    return 0


def run(args: T.List[str]) -> int:
    if args and args[0] == '--p1689':
        return run_p1689(args[1:])

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
