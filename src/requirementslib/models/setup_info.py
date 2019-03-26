# -*- coding=utf-8 -*-
from __future__ import absolute_import, print_function

import atexit
import contextlib
import importlib
import os
import shutil
import sys

import attr
import packaging.specifiers
import packaging.utils
import packaging.version
import pep517.envbuild
import pep517.wrappers
import six
from appdirs import user_cache_dir
from distlib.wheel import Wheel
from packaging.markers import Marker
from six.moves import configparser
from six.moves.urllib.parse import unquote, urlparse, urlunparse
from vistir.compat import Iterable, Path, lru_cache
from vistir.contextmanagers import cd, temp_path
from vistir.misc import run
from vistir.path import create_tracked_tempdir, ensure_mkdir_p, mkdir_p, rmtree

from .utils import (
    get_default_pyproject_backend,
    get_name_variants,
    get_pyproject,
    init_requirement,
    split_vcs_method_from_uri,
    strip_extras_markers_from_requirement,
)
from ..environment import MYPY_RUNNING
from ..exceptions import RequirementError

try:
    from setuptools.dist import distutils
except ImportError:
    import distutils


try:
    from os import scandir
except ImportError:
    from scandir import scandir


if MYPY_RUNNING:
    from typing import (
        Any,
        Dict,
        List,
        Generator,
        Optional,
        Union,
        Tuple,
        TypeVar,
        Text,
        Set,
        AnyStr,
    )
    from pip_shims.shims import InstallRequirement, PackageFinder
    from pkg_resources import (
        PathMetadata,
        DistInfoDistribution,
        Requirement as PkgResourcesRequirement,
    )
    from packaging.requirements import Requirement as PackagingRequirement

    TRequirement = TypeVar("TRequirement")
    RequirementType = TypeVar(
        "RequirementType", covariant=True, bound=PackagingRequirement
    )
    MarkerType = TypeVar("MarkerType", covariant=True, bound=Marker)
    STRING_TYPE = Union[str, bytes, Text]
    S = TypeVar("S", bytes, str, Text)


CACHE_DIR = os.environ.get("PIPENV_CACHE_DIR", user_cache_dir("pipenv"))

# The following are necessary for people who like to use "if __name__" conditionals
# in their setup.py scripts
_setup_stop_after = None
_setup_distribution = None


def pep517_subprocess_runner(cmd, cwd=None, extra_environ=None):
    # type: (List[AnyStr], Optional[AnyStr], Optional[Dict[AnyStr, AnyStr]]) -> None
    """The default method of calling the wrapper subprocess."""
    env = os.environ.copy()
    if extra_environ:
        env.update(extra_environ)

    run(
        cmd,
        cwd=cwd,
        env=env,
        block=True,
        combine_stderr=True,
        return_object=False,
        write_to_stdout=False,
        nospin=True,
    )


class BuildEnv(pep517.envbuild.BuildEnvironment):
    def pip_install(self, reqs):
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-installed",
            "--prefix",
            self.path,
        ] + list(reqs)
        run(
            cmd,
            block=True,
            combine_stderr=True,
            return_object=False,
            write_to_stdout=False,
            nospin=True,
        )


class HookCaller(pep517.wrappers.Pep517HookCaller):
    def __init__(self, source_dir, build_backend):
        self.source_dir = os.path.abspath(source_dir)
        self.build_backend = build_backend
        self._subprocess_runner = pep517_subprocess_runner


def parse_special_directives(setup_entry):
    # type: (S) -> S
    rv = setup_entry
    if setup_entry.startswith("file:"):
        _, path = setup_entry.split("file:")
        path = path.strip()
        if os.path.exists(path):
            with open(path, "r") as fh:
                rv = fh.read()
    elif setup_entry.startswith("attr:"):
        _, resource = setup_entry.split("attr:")
        resource = resource.strip()
        if "." in resource:
            resource, _, attribute = resource.rpartition(".")
        module = importlib.import_module(resource)
        rv = getattr(module, attribute)
        if not isinstance(rv, six.string_types):
            rv = str(rv)
    return rv


def parse_setup_cfg(setup_cfg_path):
    # type: (S) -> Dict[S, Union[S, None, Set[BaseRequirement], List[S], Tuple[S, Tuple[BaseRequirement]]]]
    if os.path.exists(setup_cfg_path):
        default_opts = {
            "metadata": {"name": "", "version": ""},
            "options": {
                "install_requires": "",
                "python_requires": "",
                "build_requires": "",
                "setup_requires": "",
                "extras": "",
            },
        }
        parser = configparser.ConfigParser(default_opts)
        parser.read(setup_cfg_path)
        results = {}
        if parser.has_option("metadata", "name"):
            results["name"] = parse_special_directives(parser.get("metadata", "name"))
        if parser.has_option("metadata", "version"):
            results["version"] = parse_special_directives(
                parser.get("metadata", "version")
            )
        install_requires = set()  # type: Set[BaseRequirement]
        if parser.has_option("options", "install_requires"):
            install_requires = set(
                [
                    BaseRequirement.from_string(dep)
                    for dep in parser.get("options", "install_requires").split("\n")
                    if dep
                ]
            )
        results["install_requires"] = install_requires
        if parser.has_option("options", "python_requires"):
            results["python_requires"] = parse_special_directives(
                parser.get("options", "python_requires")
            )
        if parser.has_option("options", "build_requires"):
            results["build_requires"] = parser.get("options", "build_requires")
        extras = []
        if "options.extras_require" in parser.sections():
            extras_require_section = parser.options("options.extras_require")
            for section in extras_require_section:
                if section in ["options", "metadata"]:
                    continue
                section_contents = parser.get("options.extras_require", section)
                section_list = section_contents.split("\n")
                section_extras = []
                for extra_name in section_list:
                    if not extra_name or extra_name.startswith("#"):
                        continue
                    section_extras.append(BaseRequirement.from_string(extra_name))
                if section_extras:
                    extras.append(tuple([section, tuple(section_extras)]))
        results["extras_require"] = tuple(extras)
        return results


@contextlib.contextmanager
def _suppress_distutils_logs():
    # type: () -> Generator[None, None, None]
    """Hack to hide noise generated by `setup.py develop`.

    There isn't a good way to suppress them now, so let's monky-patch.
    See https://bugs.python.org/issue25392.
    """

    f = distutils.log.Log._log

    def _log(log, level, msg, args):
        if level >= distutils.log.ERROR:
            f(log, level, msg, args)

    distutils.log.Log._log = _log
    yield
    distutils.log.Log._log = f


def build_pep517(source_dir, build_dir, config_settings=None, dist_type="wheel"):
    if config_settings is None:
        config_settings = {}
    requires, backend = get_pyproject(source_dir)
    hookcaller = HookCaller(source_dir, backend)
    if dist_type == "sdist":
        get_requires_fn = hookcaller.get_requires_for_build_sdist
        build_fn = hookcaller.build_sdist
    else:
        get_requires_fn = hookcaller.get_requires_for_build_wheel
        build_fn = hookcaller.build_wheel

    with BuildEnv() as env:
        env.pip_install(requires)
        reqs = get_requires_fn(config_settings)
        env.pip_install(reqs)
        return build_fn(build_dir, config_settings)


@ensure_mkdir_p(mode=0o775)
def _get_src_dir(root):
    # type: (AnyStr) -> AnyStr
    src = os.environ.get("PIP_SRC")
    if src:
        return src
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env is not None:
        return os.path.join(virtual_env, "src")
    if root is not None:
        # Intentionally don't match pip's behavior here -- this is a temporary copy
        src_dir = create_tracked_tempdir(prefix="requirementslib-", suffix="-src")
    else:
        src_dir = os.path.join(root, "src")
    return src_dir


@lru_cache()
def ensure_reqs(reqs):
    # type: (List[Union[S, PkgResourcesRequirement]]) -> List[PkgResourcesRequirement]
    import pkg_resources

    if not isinstance(reqs, Iterable):
        raise TypeError("Expecting an Iterable, got %r" % reqs)
    new_reqs = []
    for req in reqs:
        if not req:
            continue
        if isinstance(req, six.string_types):
            req = pkg_resources.Requirement.parse("{0}".format(str(req)))
        # req = strip_extras_markers_from_requirement(req)
        new_reqs.append(req)
    return new_reqs


def _prepare_wheel_building_kwargs(
    ireq=None, src_root=None, src_dir=None, editable=False
):
    # type: (Optional[InstallRequirement], Optional[AnyStr], Optional[AnyStr], bool) -> Dict[AnyStr, AnyStr]
    download_dir = os.path.join(CACHE_DIR, "pkgs")  # type: STRING_TYPE
    mkdir_p(download_dir)

    wheel_download_dir = os.path.join(CACHE_DIR, "wheels")  # type: STRING_TYPE
    mkdir_p(wheel_download_dir)

    if src_dir is None:
        if editable and src_root is not None:
            src_dir = src_root
        elif ireq is None and src_root is not None:
            src_dir = _get_src_dir(root=src_root)  # type: STRING_TYPE
        elif ireq is not None and ireq.editable and src_root is not None:
            src_dir = _get_src_dir(root=src_root)
        else:
            src_dir = create_tracked_tempdir(prefix="reqlib-src")

    # Let's always resolve in isolation
    if src_dir is None:
        src_dir = create_tracked_tempdir(prefix="reqlib-src")
    build_dir = create_tracked_tempdir(prefix="reqlib-build")

    return {
        "build_dir": build_dir,
        "src_dir": src_dir,
        "download_dir": download_dir,
        "wheel_download_dir": wheel_download_dir,
    }


def iter_metadata(path, pkg_name=None, metadata_type="egg-info"):
    # type: (AnyStr, Optional[AnyStr], AnyStr) -> Generator
    if pkg_name is not None:
        pkg_variants = get_name_variants(pkg_name)
    non_matching_dirs = []
    for entry in scandir(path):
        if entry.is_dir():
            entry_name, ext = os.path.splitext(entry.name)
            if ext.endswith(metadata_type):
                if pkg_name is None or entry_name.lower() in pkg_variants:
                    yield entry
            elif not entry.name.endswith(metadata_type):
                non_matching_dirs.append(entry)
    for entry in non_matching_dirs:
        for dir_entry in iter_metadata(
            entry.path, pkg_name=pkg_name, metadata_type=metadata_type
        ):
            yield dir_entry


def find_egginfo(target, pkg_name=None):
    # type: (AnyStr, Optional[AnyStr]) -> Generator
    egg_dirs = (
        egg_dir
        for egg_dir in iter_metadata(target, pkg_name=pkg_name)
        if egg_dir is not None
    )
    if pkg_name:
        yield next(iter(eggdir for eggdir in egg_dirs if eggdir is not None), None)
    else:
        for egg_dir in egg_dirs:
            yield egg_dir


def find_distinfo(target, pkg_name=None):
    # type: (AnyStr, Optional[AnyStr]) -> Generator
    dist_dirs = (
        dist_dir
        for dist_dir in iter_metadata(
            target, pkg_name=pkg_name, metadata_type="dist-info"
        )
        if dist_dir is not None
    )
    if pkg_name:
        yield next(iter(dist for dist in dist_dirs if dist is not None), None)
    else:
        for dist_dir in dist_dirs:
            yield dist_dir


def get_metadata(path, pkg_name=None, metadata_type=None):
    # type: (S, Optional[S], Optional[S]) -> Dict[S, Union[S, List[RequirementType], Dict[S, RequirementType]]]
    metadata_dirs = []
    wheel_allowed = metadata_type == "wheel" or metadata_type is None
    egg_allowed = metadata_type == "egg" or metadata_type is None
    egg_dir = next(iter(find_egginfo(path, pkg_name=pkg_name)), None)
    dist_dir = next(iter(find_distinfo(path, pkg_name=pkg_name)), None)
    if dist_dir and wheel_allowed:
        metadata_dirs.append(dist_dir)
    if egg_dir and egg_allowed:
        metadata_dirs.append(egg_dir)
    matched_dir = next(iter(d for d in metadata_dirs if d is not None), None)
    metadata_dir = None
    base_dir = None
    if matched_dir is not None:
        import pkg_resources

        metadata_dir = os.path.abspath(matched_dir.path)
        base_dir = os.path.dirname(metadata_dir)
        dist = None
        distinfo_dist = None
        egg_dist = None
        if wheel_allowed and dist_dir is not None:
            distinfo_dist = next(iter(pkg_resources.find_distributions(base_dir)), None)
        if egg_allowed and egg_dir is not None:
            path_metadata = pkg_resources.PathMetadata(base_dir, metadata_dir)
            egg_dist = next(
                iter(pkg_resources.distributions_from_metadata(path_metadata.egg_info)),
                None,
            )
        dist = next(iter(d for d in (distinfo_dist, egg_dist) if d is not None), None)
        if dist is not None:
            return get_metadata_from_dist(dist)
    return {}


@lru_cache()
def get_extra_name_from_marker(marker):
    # type: (MarkerType) -> Optional[S]
    if not marker:
        raise ValueError("Invalid value for marker: {0!r}".format(marker))
    if not getattr(marker, "_markers", None):
        raise TypeError("Expecting a marker instance, received {0!r}".format(marker))
    for elem in marker._markers:
        if isinstance(elem, tuple) and elem[0].value == "extra":
            return elem[2].value
    return None


def get_metadata_from_wheel(wheel_path):
    # type: (S) -> Dict[Any, Any]
    if not isinstance(wheel_path, six.string_types):
        raise TypeError("Expected string instance, received {0!r}".format(wheel_path))
    try:
        dist = Wheel(wheel_path)
    except Exception:
        pass
    metadata = dist.metadata
    name = metadata.name
    version = metadata.version
    requires = []
    extras_keys = getattr(metadata, "extras", [])
    extras = {k: [] for k in extras_keys}
    for req in getattr(metadata, "run_requires", []):
        parsed_req = init_requirement(req)
        parsed_marker = parsed_req.marker
        if parsed_marker:
            extra = get_extra_name_from_marker(parsed_marker)
            if extra is None:
                requires.append(parsed_req)
                continue
            if extra not in extras:
                extras[extra] = []
            parsed_req = strip_extras_markers_from_requirement(parsed_req)
            extras[extra].append(parsed_req)
        else:
            requires.append(parsed_req)
    return {"name": name, "version": version, "requires": requires, "extras": extras}


def get_metadata_from_dist(dist):
    # type: (Union[PathMetadata, DistInfoDistribution]) -> Dict[S, Union[S, List[RequirementType], Dict[S, RequirementType]]]
    try:
        requires = dist.requires()
    except Exception:
        requires = []
    try:
        dep_map = dist._build_dep_map()
    except Exception:
        dep_map = {}
    deps = []
    extras = {}
    for k in dep_map.keys():
        if k is None:
            deps.extend(dep_map.get(k))
            continue
        else:
            extra = None
            _deps = dep_map.get(k)
            if k.startswith(":python_version"):
                marker = k.replace(":", "; ")
            else:
                marker = ""
                extra = "{0}".format(k)
            _deps = ["{0}{1}".format(str(req), marker) for req in _deps]
            _deps = ensure_reqs(tuple(_deps))
            if extra:
                extras[extra] = _deps
            else:
                deps.extend(_deps)
    return {
        "name": dist.project_name,
        "version": dist.version,
        "requires": requires,
        "extras": extras,
    }


@attr.s(slots=True, frozen=True)
class BaseRequirement(object):
    name = attr.ib(default="", cmp=True)  # type: STRING_TYPE
    requirement = attr.ib(
        default=None, cmp=True
    )  # type: Optional[PkgResourcesRequirement]

    def __str__(self):
        # type: () -> S
        return "{0}".format(str(self.requirement))

    def as_dict(self):
        # type: () -> Dict[S, Optional[PkgResourcesRequirement]]
        return {self.name: self.requirement}

    def as_tuple(self):
        # type: () -> Tuple[S, Optional[PkgResourcesRequirement]]
        return (self.name, self.requirement)

    @classmethod
    @lru_cache()
    def from_string(cls, line):
        # type: (S) -> BaseRequirement
        line = line.strip()
        req = init_requirement(line)
        return cls.from_req(req)

    @classmethod
    @lru_cache()
    def from_req(cls, req):
        # type: (PkgResourcesRequirement) -> BaseRequirement
        name = None
        key = getattr(req, "key", None)
        name = getattr(req, "name", None)
        project_name = getattr(req, "project_name", None)
        if key is not None:
            name = key
        if name is None:
            name = project_name
        return cls(name=name, requirement=req)


@attr.s(slots=True, frozen=True)
class Extra(object):
    name = attr.ib(default=None, cmp=True)  # type: STRING_TYPE
    requirements = attr.ib(factory=frozenset, cmp=True, type=frozenset)

    def __str__(self):
        # type: () -> S
        return "{0}: {{{1}}}".format(
            self.section, ", ".join([r.name for r in self.requirements])
        )

    def add(self, req):
        # type: (BaseRequirement) -> None
        if req not in self.requirements:
            return attr.evolve(
                self, requirements=frozenset(set(self.requirements).add(req))
            )
        return self

    def as_dict(self):
        # type: () -> Dict[S, Tuple[RequirementType, ...]]
        return {self.name: tuple([r.requirement for r in self.requirements])}


@attr.s(slots=True, cmp=True, hash=True)
class SetupInfo(object):
    name = attr.ib(default=None, cmp=True)  # type: STRING_TYPE
    base_dir = attr.ib(default=None, cmp=True, hash=False)  # type: STRING_TYPE
    version = attr.ib(default=None, cmp=True)  # type: STRING_TYPE
    _requirements = attr.ib(type=frozenset, factory=frozenset, cmp=True, hash=True)
    build_requires = attr.ib(type=tuple, default=attr.Factory(tuple), cmp=True)
    build_backend = attr.ib(cmp=True)  # type: STRING_TYPE
    setup_requires = attr.ib(type=tuple, default=attr.Factory(tuple), cmp=True)
    python_requires = attr.ib(
        type=packaging.specifiers.SpecifierSet, default=None, cmp=True
    )
    _extras_requirements = attr.ib(type=tuple, default=attr.Factory(tuple), cmp=True)
    setup_cfg = attr.ib(type=Path, default=None, cmp=True, hash=False)
    setup_py = attr.ib(type=Path, default=None, cmp=True, hash=False)
    pyproject = attr.ib(type=Path, default=None, cmp=True, hash=False)
    ireq = attr.ib(
        default=None, cmp=True, hash=False
    )  # type: Optional[InstallRequirement]
    extra_kwargs = attr.ib(default=attr.Factory(dict), type=dict, cmp=False, hash=False)
    metadata = attr.ib(default=None)  # type: Optional[Tuple[STRING_TYPE]]

    @build_backend.default
    def get_build_backend(self):
        # type: () -> S
        return get_default_pyproject_backend()

    @property
    def requires(self):
        # type: () -> Dict[S, RequirementType]
        return {req.name: req.requirement for req in self._requirements}

    @property
    def extras(self):
        # type: () -> Dict[S, Optional[Any]]
        extras_dict = {}
        extras = set(self._extras_requirements)
        for section, deps in extras:
            if isinstance(deps, BaseRequirement):
                extras_dict[section] = deps.requirement
            elif isinstance(deps, (list, tuple)):
                extras_dict[section] = [d.requirement for d in deps]
        return extras_dict

    @classmethod
    def get_setup_cfg(cls, setup_cfg_path):
        # type: (S) -> Dict[S, Union[S, None, Set[BaseRequirement], List[S], Tuple[S, Tuple[BaseRequirement]]]]
        return parse_setup_cfg(setup_cfg_path)

    @property
    def egg_base(self):
        # type: () -> S
        base = None  # type: Optional[STRING_TYPE]
        if self.setup_py.exists():
            base = self.setup_py.parent
        elif self.pyproject.exists():
            base = self.pyproject.parent
        elif self.setup_cfg.exists():
            base = self.setup_cfg.parent
        if base is None:
            base = Path(self.base_dir)
        if base is None:
            base = Path(self.extra_kwargs["src_dir"])
        egg_base = base.joinpath("reqlib-metadata")
        if not egg_base.exists():
            atexit.register(rmtree, egg_base.as_posix())
        egg_base.mkdir(parents=True, exist_ok=True)
        return egg_base.as_posix()

    def parse_setup_cfg(self):
        # type: () -> None
        if self.setup_cfg is not None and self.setup_cfg.exists():
            parsed = self.get_setup_cfg(self.setup_cfg.as_posix())
            if self.name is None:
                self.name = parsed.get("name")
            if self.version is None:
                self.version = parsed.get("version")
            build_requires = parsed.get("build_requires", [])
            if self.build_requires:
                self.build_requires = tuple(
                    set(self.build_requires) | set(build_requires)
                )
            self._requirements = frozenset(
                set(self._requirements) | set(parsed["install_requires"])
            )
            if self.python_requires is None:
                self.python_requires = parsed.get("python_requires")
            if not self._extras_requirements:
                self._extras_requirements = parsed["extras_require"]
            else:
                self._extras_requirements = (
                    self._extras_requirements + parsed["extras_require"]
                )
            if self.ireq is not None and self.ireq.extras:
                for extra in self.ireq.extras:
                    if extra in self.extras:
                        extras_tuple = tuple(
                            [BaseRequirement.from_req(req) for req in self.extras[extra]]
                        )
                        self._extras_requirements += ((extra, extras_tuple),)
                        self._requirements = frozenset(
                            set(self._requirements) | set(list(extras_tuple))
                        )

    def run_setup(self):
        # type: () -> None
        if self.setup_py is not None and self.setup_py.exists():
            target_cwd = self.setup_py.parent.as_posix()
            with temp_path(), cd(target_cwd), _suppress_distutils_logs():
                # This is for you, Hynek
                # see https://github.com/hynek/environ_config/blob/69b1c8a/setup.py
                script_name = self.setup_py.as_posix()
                args = ["egg_info", "--egg-base", self.egg_base]
                g = {"__file__": script_name, "__name__": "__main__"}
                sys.path.insert(0, os.path.dirname(os.path.abspath(script_name)))
                local_dict = {}
                if sys.version_info < (3, 5):
                    save_argv = sys.argv
                else:
                    save_argv = sys.argv.copy()
                try:
                    global _setup_distribution, _setup_stop_after
                    _setup_stop_after = "run"
                    sys.argv[0] = script_name
                    sys.argv[1:] = args
                    with open(script_name, "rb") as f:
                        if sys.version_info < (3, 5):
                            exec(f.read(), g, local_dict)
                        else:
                            exec(f.read(), g)
                # We couldn't import everything needed to run setup
                except NameError:
                    python = os.environ.get("PIP_PYTHON_PATH", sys.executable)
                    out, _ = run(
                        [python, "setup.py"] + args,
                        cwd=target_cwd,
                        block=True,
                        combine_stderr=False,
                        return_object=False,
                        nospin=True,
                    )
                finally:
                    _setup_stop_after = None
                    sys.argv = save_argv
                dist = _setup_distribution
                if not dist:
                    self.get_egg_metadata()
                    return

                name = dist.get_name()
                if name:
                    self.name = name
                if dist.python_requires and not self.python_requires:
                    self.python_requires = packaging.specifiers.SpecifierSet(
                        dist.python_requires
                    )
                if not self._extras_requirements:
                    self._extras_requirements = ()
                if dist.extras_require and not self.extras:
                    for extra, extra_requires in dist.extras_require:
                        extras_tuple = tuple(
                            BaseRequirement.from_req(req) for req in extra_requires
                        )
                        self._extras_requirements += ((extra, extras_tuple),)
                install_requires = dist.get_requires()
                if not install_requires:
                    install_requires = dist.install_requires
                if install_requires and not self.requires:
                    requirements = set(
                        [BaseRequirement.from_req(req) for req in install_requires]
                    )
                    if getattr(self.ireq, "extras", None):
                        for extra in self.ireq.extras:
                            requirements |= set(list(self.extras.get(extra, [])))
                    self._requirements = frozenset(set(self._requirements) | requirements)
                if dist.setup_requires and not self.setup_requires:
                    self.setup_requires = tuple(dist.setup_requires)
                if not self.version:
                    self.version = dist.get_version()

    @property
    @lru_cache()
    def pep517_config(self):
        config = {}
        config.setdefault("--global-option", [])
        return config

    def build_wheel(self):
        # type: () -> S
        if not self.pyproject.exists():
            build_requires = ", ".join(['"{0}"'.format(r) for r in self.build_requires])
            self.pyproject.write_text(
                u"""
[build-system]
requires = [{0}]
build-backend = "{1}"
            """.format(
                    build_requires, self.build_backend
                ).strip()
            )
        return build_pep517(
            self.base_dir,
            self.extra_kwargs["build_dir"],
            config_settings=self.pep517_config,
            dist_type="wheel",
        )

    # noinspection PyPackageRequirements
    def build_sdist(self):
        # type: () -> S
        if not self.pyproject.exists():
            build_requires = ", ".join(['"{0}"'.format(r) for r in self.build_requires])
            self.pyproject.write_text(
                u"""
[build-system]
requires = [{0}]
build-backend = "{1}"
            """.format(
                    build_requires, self.build_backend
                ).strip()
            )
        return build_pep517(
            self.base_dir,
            self.extra_kwargs["build_dir"],
            config_settings=self.pep517_config,
            dist_type="sdist",
        )

    def build(self):
        # type: () -> None
        dist_path = None
        try:
            dist_path = self.build_wheel()
        except Exception:
            try:
                dist_path = self.build_sdist()
                self.get_egg_metadata(metadata_type="egg")
            except Exception:
                pass
        else:
            self.get_metadata_from_wheel(
                os.path.join(self.extra_kwargs["build_dir"], dist_path)
            )
        if not self.metadata or not self.name:
            self.get_egg_metadata()
        if not self.metadata or not self.name:
            self.run_setup()
        return None

    def reload(self):
        # type: () -> Dict[S, Any]
        """
        Wipe existing distribution info metadata for rebuilding.
        """
        for metadata_dir in os.listdir(self.egg_base):
            shutil.rmtree(metadata_dir, ignore_errors=True)
        self.metadata = None
        self._requirements = frozenset()
        self._extras_requirements = ()
        self.get_info()

    def get_metadata_from_wheel(self, wheel_path):
        # type: (S) -> Dict[Any, Any]
        metadata_dict = get_metadata_from_wheel(wheel_path)
        if metadata_dict:
            self.populate_metadata(metadata_dict)

    def get_egg_metadata(self, metadata_dir=None, metadata_type=None):
        # type: (Optional[AnyStr], Optional[AnyStr]) -> None
        package_indicators = [self.pyproject, self.setup_py, self.setup_cfg]
        # if self.setup_py is not None and self.setup_py.exists():
        metadata_dirs = []
        if any([fn is not None and fn.exists() for fn in package_indicators]):
            metadata_dirs = [
                self.extra_kwargs["build_dir"],
                self.egg_base,
                self.extra_kwargs["src_dir"],
            ]
        if metadata_dir is not None:
            metadata_dirs = [metadata_dir] + metadata_dirs
        metadata = [
            get_metadata(d, pkg_name=self.name, metadata_type=metadata_type)
            for d in metadata_dirs
            if os.path.exists(d)
        ]
        metadata = next(iter(d for d in metadata if d), None)
        if metadata is not None:
            self.populate_metadata(metadata)

    def populate_metadata(self, metadata):
        # type: (Dict[Any, Any]) -> None
        _metadata = ()
        for k, v in metadata.items():
            if k == "extras" and isinstance(v, dict):
                extras = ()
                for extra, reqs in v.items():
                    extras += ((extra, tuple(reqs)),)
                _metadata += extras
            elif isinstance(v, (list, tuple)):
                _metadata += (k, tuple(v))
            else:
                _metadata += (k, v)
        self.metadata = _metadata
        if self.name is None:
            self.name = metadata.get("name", self.name)
        if not self.version:
            self.version = metadata.get("version", self.version)
        self._requirements = frozenset(
            set(self._requirements)
            | set([BaseRequirement.from_req(req) for req in metadata.get("requires", [])])
        )
        if getattr(self.ireq, "extras", None):
            for extra in self.ireq.extras:
                extras = metadata.get("extras", {}).get(extra, [])
                if extras:
                    extras_tuple = tuple(
                        [
                            BaseRequirement.from_req(req)
                            for req in ensure_reqs(tuple(extras))
                            if req is not None
                        ]
                    )
                    self._extras_requirements += ((extra, extras_tuple),)
                    self._requirements = frozenset(
                        set(self._requirements) | set(extras_tuple)
                    )

    def run_pyproject(self):
        # type: () -> None
        if self.pyproject and self.pyproject.exists():
            result = get_pyproject(self.pyproject.parent)
            if result is not None:
                requires, backend = result
                if backend:
                    self.build_backend = backend
                else:
                    self.build_backend = get_default_pyproject_backend()
                if requires:
                    self.build_requires = tuple(set(requires) | set(self.build_requires))
                else:
                    self.build_requires = ("setuptools", "wheel")

    def get_info(self):
        # type: () -> Dict[S, Any]
        if self.setup_cfg and self.setup_cfg.exists():
            with cd(self.base_dir):
                self.parse_setup_cfg()

        with cd(self.base_dir):
            self.run_pyproject()
            self.build()

        if self.setup_py and self.setup_py.exists() and self.metadata is None:
            if not self.requires or not self.name:
                try:
                    with cd(self.base_dir):
                        self.run_setup()
                except Exception:
                    with cd(self.base_dir):
                        self.get_egg_metadata()
                if self.metadata is None or not self.name:
                    with cd(self.base_dir):
                        self.get_egg_metadata()

        return self.as_dict()

    def as_dict(self):
        # type: () -> Dict[S, Any]
        prop_dict = {
            "name": self.name,
            "version": self.version,
            "base_dir": self.base_dir,
            "ireq": self.ireq,
            "build_backend": self.build_backend,
            "build_requires": self.build_requires,
            "requires": self.requires,
            "setup_requires": self.setup_requires,
            "python_requires": self.python_requires,
            "extras": self.extras,
            "extra_kwargs": self.extra_kwargs,
            "setup_cfg": self.setup_cfg,
            "setup_py": self.setup_py,
            "pyproject": self.pyproject,
        }
        return {k: v for k, v in prop_dict.items() if v}

    @classmethod
    def from_requirement(cls, requirement, finder=None):
        # type: (TRequirement, Optional[PackageFinder]) -> Optional[SetupInfo]
        ireq = requirement.as_ireq()
        subdir = getattr(requirement.req, "subdirectory", None)
        return cls.from_ireq(ireq, subdir=subdir, finder=finder)

    @classmethod
    @lru_cache()
    def from_ireq(cls, ireq, subdir=None, finder=None):
        # type: (InstallRequirement, Optional[AnyStr], Optional[PackageFinder]) -> Optional[SetupInfo]
        import pip_shims.shims

        if not ireq.link:
            return
        if ireq.link.is_wheel:
            return
        if not finder:
            from .dependencies import get_finder

            finder = get_finder()
        _, uri = split_vcs_method_from_uri(unquote(ireq.link.url_without_fragment))
        parsed = urlparse(uri)
        if "file" in parsed.scheme:
            url_path = parsed.path
            if "@" in url_path:
                url_path, _, _ = url_path.rpartition("@")
            parsed = parsed._replace(path=url_path)
            uri = urlunparse(parsed)
        path = None
        if ireq.link.scheme == "file" or uri.startswith("file://"):
            if "file:/" in uri and "file:///" not in uri:
                uri = uri.replace("file:/", "file:///")
            path = pip_shims.shims.url_to_path(uri)
        kwargs = _prepare_wheel_building_kwargs(ireq)
        ireq.source_dir = kwargs["src_dir"]
        if not (
            ireq.editable
            and pip_shims.shims.is_file_url(ireq.link)
            and not ireq.link.is_artifact
        ):
            if ireq.is_wheel:
                only_download = True
                download_dir = kwargs["wheel_download_dir"]
            else:
                only_download = False
                download_dir = kwargs["download_dir"]
        elif path is not None and os.path.isdir(path):
            raise RequirementError(
                "The file URL points to a directory not installable: {}".format(ireq.link)
            )
        ireq.build_location(kwargs["build_dir"])
        src_dir = ireq.ensure_has_source_dir(kwargs["src_dir"])
        ireq._temp_build_dir.path = kwargs["build_dir"]

        ireq.populate_link(finder, False, False)
        pip_shims.shims.unpack_url(
            ireq.link,
            src_dir,
            download_dir,
            only_download=only_download,
            session=finder.session,
            hashes=ireq.hashes(False),
            progress_bar="off",
        )
        created = cls.create(src_dir, subdirectory=subdir, ireq=ireq, kwargs=kwargs)
        return created

    @classmethod
    def create(cls, base_dir, subdirectory=None, ireq=None, kwargs=None):
        # type: (AnyStr, Optional[AnyStr], Optional[InstallRequirement], Optional[Dict[AnyStr, AnyStr]]) -> Optional[SetupInfo]
        if not base_dir or base_dir is None:
            return

        creation_kwargs = {"extra_kwargs": kwargs}
        if not isinstance(base_dir, Path):
            base_dir = Path(base_dir)
        creation_kwargs["base_dir"] = base_dir.as_posix()
        pyproject = base_dir.joinpath("pyproject.toml")

        if subdirectory is not None:
            base_dir = base_dir.joinpath(subdirectory)
        setup_py = base_dir.joinpath("setup.py")
        setup_cfg = base_dir.joinpath("setup.cfg")
        creation_kwargs["pyproject"] = pyproject
        creation_kwargs["setup_py"] = setup_py
        creation_kwargs["setup_cfg"] = setup_cfg
        if ireq:
            creation_kwargs["ireq"] = ireq
        created = cls(**creation_kwargs)
        created.get_info()
        return created
