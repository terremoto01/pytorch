import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import torch
import warnings
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen, Request
from urllib.parse import urlparse  # noqa: F401

try:
    from tqdm.auto import tqdm  # automatically select proper tqdm submodule if available
except ImportError:
    try:
        from tqdm import tqdm
    except ImportError:
        # fake tqdm if it's not installed
        class tqdm(object):  # type: ignore[no-redef]

            def __init__(self, total=None, disable=False,
                         unit=None, unit_scale=None, unit_divisor=None):
                self.total = total
                self.disable = disable
                self.n = 0
                # ignore unit, unit_scale, unit_divisor; they're just for real tqdm

            def update(self, n):
                if self.disable:
                    return

                self.n += n
                if self.total is None:
                    sys.stderr.write("\r{0:.1f} bytes".format(self.n))
                else:
                    sys.stderr.write("\r{0:.1f}%".format(100 * self.n / float(self.total)))
                sys.stderr.flush()

            def close(self):
                self.disable = True

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.disable:
                    return

                sys.stderr.write('\n')

# matches bfd8deac from resnet18-bfd8deac.pth
HASH_REGEX = re.compile(r'-([a-f0-9]*)\.')

_PREDEFINED_TRUSTED = ("facebookresearch", "facebookincubator", "pytorch", "fairinternal")
ENV_GITHUB_TOKEN = 'GITHUB_TOKEN'
ENV_TORCH_HOME = 'TORCH_HOME'
ENV_XDG_CACHE_HOME = 'XDG_CACHE_HOME'
DEFAULT_CACHE_DIR = '~/.cache'
VAR_DEPENDENCY = 'dependencies'
MODULE_HUBCONF = 'hubconf.py'
READ_DATA_CHUNK = 8192
_hub_dir = None


# Copied from tools/shared/module_loader to be included in torch package
def _import_module(name, path):
    import importlib.util
    from importlib.abc import Loader
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert isinstance(spec.loader, Loader)
    spec.loader.exec_module(module)
    return module


def import_module(name, path):
    warnings.warn('The use of torch.hub.import_module is deprecated in v0.11 and will be removed in v0.12', DeprecationWarning)
    return _import_module(name, path)


def _remove_if_exists(path):
    if os.path.exists(path):
        if os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path)


def _git_archive_link(repo_owner, repo_name, branch):
    return 'https://github.com/{}/{}/archive/{}.zip'.format(repo_owner, repo_name, branch)


def _load_attr_from_module(module, func_name):
    # Check if callable is defined in the module
    if func_name not in dir(module):
        return None
    return getattr(module, func_name)


def _get_torch_home():
    torch_home = os.path.expanduser(
        os.getenv(ENV_TORCH_HOME,
                  os.path.join(os.getenv(ENV_XDG_CACHE_HOME,
                                         DEFAULT_CACHE_DIR), 'torch')))
    return torch_home


def _parse_repo_info(github):
    if ':' in github:
        repo_info, branch = github.split(':')
    else:
        repo_info, branch = github, None
    repo_owner, repo_name = repo_info.split('/')

    if branch is None:
        # The branch wasn't specified by the user, so we need to figure out the
        # default branch: main or master. Our assumption is that if main exists
        # then it's the default branch, otherwise it's master.
        try:
            with urlopen(f"https://github.com/{repo_owner}/{repo_name}/tree/main/"):
                branch = 'main'
        except HTTPError as e:
            if e.code == 404:
                branch = 'master'
            else:
                raise
    return repo_owner, repo_name, branch


def _read_url(url):
    with urlopen(url) as r:
        return r.read().decode(r.headers.get_content_charset('utf-8'))


def _validate_not_a_forked_repo(repo_owner, repo_name, branch):
    # Use urlopen to avoid depending on local git.
    headers = {'Accept': 'application/vnd.github.v3+json'}
    token = os.environ.get(ENV_GITHUB_TOKEN)
    if token is not None:
        headers['Authorization'] = f'token {token}'
    for url_prefix in (
            f'https://api.github.com/repos/{repo_owner}/{repo_name}/branches',
            f'https://api.github.com/repos/{repo_owner}/{repo_name}/tags'):
        page = 0
        while True:
            page += 1
            url = f'{url_prefix}?per_page=100&page={page}'
            response = json.loads(_read_url(Request(url, headers=headers)))
            # Empty response means no more data to process
            if not response:
                break
            for br in response:
                if br['name'] == branch or br['commit']['sha'].startswith(branch):
                    return

    raise ValueError(f'Cannot find {branch} in https://github.com/{repo_owner}/{repo_name}. '
                     'If it\'s a commit from a forked repo, please call hub.load() with forked repo directly.')


def _get_cache_or_reload(github, force_reload, verbose=True, skip_validation=False, trust_repo=None, calling_fn="load"):
    # Setup hub_dir to save downloaded files
    hub_dir = get_dir()
    if not os.path.exists(hub_dir):
        os.makedirs(hub_dir)
    # Parse github repo information
    repo_owner, repo_name, branch = _parse_repo_info(github)
    # Github allows branch name with slash '/',
    # this causes confusion with path on both Linux and Windows.
    # Backslash is not allowed in Github branch name so no need to
    # to worry about it.
    normalized_br = branch.replace('/', '_')
    # Github renames folder repo-v1.x.x to repo-1.x.x
    # We don't know the repo name before downloading the zip file
    # and inspect name from it.
    # To check if cached repo exists, we need to normalize folder names.
    repo_dir = os.path.join(hub_dir, '_'.join([repo_owner, repo_name, normalized_br]))
    # Check that the repo is in the trusted list
    _check_repo('_'.join([repo_owner, repo_name]), trust_repo=trust_repo, calling_fn=calling_fn)

    use_cache = (not force_reload) and os.path.exists(repo_dir)

    if use_cache:
        if verbose:
            sys.stderr.write('Using cache found in {}\n'.format(repo_dir))
    else:
        # Validate the tag/branch is from the original repo instead of a forked repo
        if not skip_validation:
            _validate_not_a_forked_repo(repo_owner, repo_name, branch)

        cached_file = os.path.join(hub_dir, normalized_br + '.zip')
        _remove_if_exists(cached_file)

        url = _git_archive_link(repo_owner, repo_name, branch)
        sys.stderr.write('Downloading: \"{}\" to {}\n'.format(url, cached_file))
        download_url_to_file(url, cached_file, progress=False)

        with zipfile.ZipFile(cached_file) as cached_zipfile:
            extraced_repo_name = cached_zipfile.infolist()[0].filename
            extracted_repo = os.path.join(hub_dir, extraced_repo_name)
            _remove_if_exists(extracted_repo)
            # Unzip the code and rename the base folder
            cached_zipfile.extractall(hub_dir)

        _remove_if_exists(cached_file)
        _remove_if_exists(repo_dir)
        shutil.move(extracted_repo, repo_dir)  # rename the repo

    return repo_dir



def _add_repo_to_trusted_list(repo, filepath, is_trusted=False, prompt=True):
    if prompt:
        # prompt
        response = input(
            f"The repository {repo} does not belong to the list of trusted repositories and as such cannot be downloaded. "
            "Do you trust this repository and wish to add it to the trusted list of repositories (y/N)?")
    else:
        response = "y"
    if response.lower() in ("y", "yes"):
        if is_trusted:
            print("The repository is already trusted.")
            return
        else:
            with open(filepath, "a") as file:
                file.write(repo + "\n")

    elif response.lower() in ("n", "no", ""):
        raise Exception("Untrusted repository.")

    else:
        raise ValueError(f"Unrecognized response {response}.")


def _check_repo(repo, trust_repo=None, calling_fn="load"):
    hub_dir = get_dir()
    filepath = os.path.join(hub_dir, "trusted_list")

    if not os.path.exists(filepath):
        Path(filepath).touch()

        # Initialize with all existing repos
        repos = next(os.walk(hub_dir))[1]
        with open(filepath, "a") as file:
            for _repo in repos:
                file.write(_repo + "\n")

    # load list
    trusted_repos = [trusted_repo for trusted_repo in _PREDEFINED_TRUSTED]
    with open(filepath, 'r') as file:
        trusted_repos += file.readlines()
    is_trusted = any(trusted_repo == repo for trusted_repo in trusted_repos)

    # to be deprecated
    if trust_repo is None:
        if not is_trusted:
            warnings.warn(
                "You are about to download an untrusted repository. In a future release, this won't be allowed. "
                f"To add the repository to your trusted list, change the command to {calling_fn}(..., "
                "trust_repo=False) and a command prompt will appear asking for an explicit confirmation of trust, "
                f"or {calling_fn}(..., trust_repo=True), which will assume that the prompt is to be answered with "
                f"'yes'.")
        return

    if not trust_repo:
        _add_repo_to_trusted_list(repo, filepath, is_trusted)

    elif trust_repo == "check" and not is_trusted:
        _add_repo_to_trusted_list(repo, filepath)
    elif trust_repo and not is_trusted:
        _add_repo_to_trusted_list(repo, filepath, prompt=False)


def _check_module_exists(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _check_dependencies(m):
    dependencies = _load_attr_from_module(m, VAR_DEPENDENCY)

    if dependencies is not None:
        missing_deps = [pkg for pkg in dependencies if not _check_module_exists(pkg)]
        if len(missing_deps):
            raise RuntimeError('Missing dependencies: {}'.format(', '.join(missing_deps)))


def _load_entry_from_hubconf(m, model):
    if not isinstance(model, str):
        raise ValueError('Invalid input: model should be a string of function name')

    # Note that if a missing dependency is imported at top level of hubconf, it will
    # throw before this function. It's a chicken and egg situation where we have to
    # load hubconf to know what're the dependencies, but to import hubconf it requires
    # a missing package. This is fine, Python will throw proper error message for users.
    _check_dependencies(m)

    func = _load_attr_from_module(m, model)

    if func is None or not callable(func):
        raise RuntimeError('Cannot find callable {} in hubconf'.format(model))

    return func


def get_dir():
    r"""
    Get the Torch Hub cache directory used for storing downloaded models & weights.

    If :func:`~torch.hub.set_dir` is not called, default path is ``$TORCH_HOME/hub`` where
    environment variable ``$TORCH_HOME`` defaults to ``$XDG_CACHE_HOME/torch``.
    ``$XDG_CACHE_HOME`` follows the X Design Group specification of the Linux
    filesystem layout, with a default value ``~/.cache`` if the environment
    variable is not set.
    """
    # Issue warning to move data if old env is set
    if os.getenv('TORCH_HUB'):
        warnings.warn('TORCH_HUB is deprecated, please use env TORCH_HOME instead')

    if _hub_dir is not None:
        return _hub_dir
    return os.path.join(_get_torch_home(), 'hub')


def set_dir(d):
    r"""
    Optionally set the Torch Hub directory used to save downloaded models & weights.

    Args:
        d (string): path to a local folder to save downloaded models & weights.
    """
    global _hub_dir
    _hub_dir = d


def list(github, force_reload=False, skip_validation=False, trust_repo=None):
    r"""
    List all callable entrypoints available in the repo specified by ``github``.

    Args:
        github (string): a string with format "repo_owner/repo_name[:tag_name]" with an optional
            tag/branch. If ``tag_name`` is not specified, the default branch is assumed to be ``main`` if
            it exists, and otherwise ``master``.
            Example: 'pytorch/vision:0.10'
        force_reload (bool, optional): whether to discard the existing cache and force a fresh download.
            Default is ``False``.
        skip_validation (bool, optional): if ``False``, torchhub will check that the branch or commit
            specified by the ``github`` argument properly belongs to the repo owner. This will make
            requests to the GitHub API; you can specify a non-default GitHub token by setting the
            ``GITHUB_TOKEN`` environment variable. Default is ``False``.
        trust_repo (bool, string or None): ``"check"``, ``True``, ``False`` or ``None``.
            If ``False``, a prompt will appear and ask the user whether that repo should be trusted from now on.
            If ``"check"``, the repo address will be checked in against the list of trusted repos in the cache. If it is
            not present in that list, the behaviour will fall back onto the ``trust_repo=False`` option.
            If ``True``, the repo will be added to the trusted list and loaded without requiring explicit confirmation.
            of trust.
            If ``None``, the presence of the repo in the trusted will be checked. If that check fails, the user will be
            notified through a warning that the repo has not been formally added to the trusted list. In a future
            release, that this behaviour will be deprecated in favour of ``"check"``.
            Default is ``None``.

    Returns:
        list: The available callables entrypoint

    Example:
        >>> entrypoints = torch.hub.list('pytorch/vision', force_reload=True)
    """
    repo_dir = _get_cache_or_reload(github, force_reload, verbose=True, skip_validation=skip_validation,
                                    trust_repo=trust_repo, calling_fn="list")

    sys.path.insert(0, repo_dir)

    hubconf_path = os.path.join(repo_dir, MODULE_HUBCONF)
    hub_module = _import_module(MODULE_HUBCONF, hubconf_path)

    sys.path.remove(repo_dir)

    # We take functions starts with '_' as internal helper functions
    entrypoints = [f for f in dir(hub_module) if callable(getattr(hub_module, f)) and not f.startswith('_')]

    return entrypoints


def help(github, model, force_reload=False, skip_validation=False, trust_repo=None):
    r"""
    Show the docstring of entrypoint ``model``.

    Args:
        github (string): a string with format <repo_owner/repo_name[:tag_name]> with an optional
            tag/branch. If ``tag_name`` is not specified, the default branch is assumed to be ``main`` if
            it exists, and otherwise ``master``.
            Example: 'pytorch/vision:0.10'
        model (string): a string of entrypoint name defined in repo's ``hubconf.py``
        force_reload (bool, optional): whether to discard the existing cache and force a fresh download.
            Default is ``False``.
        skip_validation (bool, optional): if ``False``, torchhub will check that the branch or commit
            specified by the ``github`` argument properly belongs to the repo owner. This will make
            requests to the GitHub API; you can specify a non-default GitHub token by setting the
            ``GITHUB_TOKEN`` environment variable. Default is ``False``.
        trust_repo (bool, string or None): ``"check"``, ``True``, ``False`` or ``None``.
            If ``False``, a prompt will appear and ask the user whether that repo should be trusted from now on.
            If ``"check"``, the repo address will be checked in against the list of trusted repos in the cache. If it is
            not present in that list, the behaviour will fall back onto the ``trust_repo=False`` option.
            If ``True``, the repo will be added to the trusted list and loaded without requiring explicit confirmation.
            of trust.
            If ``None``, the presence of the repo in the trusted will be checked. If that check fails, the user will be
            notified through a warning that the repo has not been formally added to the trusted list. In a future
            release, that this behaviour will be deprecated in favour of ``"check"``.
            Default is ``None``.
    Example:
        >>> print(torch.hub.help('pytorch/vision', 'resnet18', force_reload=True))
    """
    repo_dir = _get_cache_or_reload(github, force_reload, verbose=True, skip_validation=skip_validation,
                                    trust_repo=trust_repo, calling_fn="help")

    sys.path.insert(0, repo_dir)

    hubconf_path = os.path.join(repo_dir, MODULE_HUBCONF)
    hub_module = _import_module(MODULE_HUBCONF, hubconf_path)

    sys.path.remove(repo_dir)

    entry = _load_entry_from_hubconf(hub_module, model)

    return entry.__doc__


def load(repo_or_dir, model, *args, source='github', trust_repo=None, force_reload=False, verbose=True,
         skip_validation=False,
         **kwargs):
    r"""
    Load a model from a github repo or a local directory.

    Note: Loading a model is the typical use case, but this can also be used to
    for loading other objects such as tokenizers, loss functions, etc.

    If ``source`` is 'github', ``repo_or_dir`` is expected to be
    of the form ``repo_owner/repo_name[:tag_name]`` with an optional
    tag/branch.

    If ``source`` is 'local', ``repo_or_dir`` is expected to be a
    path to a local directory.

    Args:
        repo_or_dir (string): If ``source`` is 'github',
            this should correspond to a github repo with format ``repo_owner/repo_name[:tag_name]`` with
            an optional tag/branch, for example 'pytorch/vision:0.10'. If ``tag_name`` is not specified,
            the default branch is assumed to be ``main`` if it exists, and otherwise ``master``.
            If ``source`` is 'local'  then it should be a path to a local directory.
        model (string): the name of a callable (entrypoint) defined in the
            repo/dir's ``hubconf.py``.
        *args (optional): the corresponding args for callable ``model``.
        source (string, optional): 'github' or 'local'. Specifies how
            ``repo_or_dir`` is to be interpreted. Default is 'github'.
        trust_repo (bool, string or None): ``"check"``, ``True``, ``False`` or ``None``.
            If ``False``, a prompt will appear and ask the user whether that repo should be trusted from now on.
            If ``"check"``, the repo address will be checked in against the list of trusted repos in the cache. If it is
            not present in that list, the behaviour will fall back onto the ``trust_repo=False`` option.
            If ``True``, the repo will be added to the trusted list and loaded without requiring explicit confirmation.
            of trust.
            If ``None``, the presence of the repo in the trusted will be checked. If that check fails, the user will be
            notified through a warning that the repo has not been formally added to the trusted list. In a future
            release, that this behaviour will be deprecated in favour of ``"check"``.
            Default is ``None``.
        force_reload (bool, optional): whether to force a fresh download of
            the github repo unconditionally. Does not have any effect if
            ``source = 'local'``. Default is ``False``.
        verbose (bool, optional): If ``False``, mute messages about hitting
            local caches. Note that the message about first download cannot be
            muted. Does not have any effect if ``source = 'local'``.
            Default is ``True``.
        skip_validation (bool, optional): if ``False``, torchhub will check that the branch or commit
            specified by the ``github`` argument properly belongs to the repo owner. This will make
            requests to the GitHub API; you can specify a non-default GitHub token by setting the
            ``GITHUB_TOKEN`` environment variable. Default is ``False``.
        **kwargs (optional): the corresponding kwargs for callable ``model``.

    Returns:
        The output of the ``model`` callable when called with the given
        ``*args`` and ``**kwargs``.

    Example:
        >>> # from a github repo
        >>> repo = 'pytorch/vision'
        >>> model = torch.hub.load(repo, 'resnet50', pretrained=True)
        >>> # from a local directory
        >>> path = '/some/local/path/pytorch/vision'
        >>> model = torch.hub.load(path, 'resnet50', pretrained=True)
    """
    source = source.lower()

    if source not in ('github', 'local'):
        raise ValueError(
            f'Unknown source: "{source}". Allowed values: "github" | "local".')

    if source == 'github':
        repo_or_dir = _get_cache_or_reload(repo_or_dir, force_reload, verbose, skip_validation,
                                           trust_repo=trust_repo, calling_fn="load")

    model = _load_local(repo_or_dir, model, *args, **kwargs)
    return model


def _load_local(hubconf_dir, model, *args, **kwargs):
    r"""
    Load a model from a local directory with a ``hubconf.py``.

    Args:
        hubconf_dir (string): path to a local directory that contains a
            ``hubconf.py``.
        model (string): name of an entrypoint defined in the directory's
            ``hubconf.py``.
        *args (optional): the corresponding args for callable ``model``.
        **kwargs (optional): the corresponding kwargs for callable ``model``.

    Returns:
        a single model with corresponding pretrained weights.

    Example:
        >>> path = '/some/local/path/pytorch/vision'
        >>> model = _load_local(path, 'resnet50', pretrained=True)
    """
    sys.path.insert(0, hubconf_dir)

    hubconf_path = os.path.join(hubconf_dir, MODULE_HUBCONF)
    hub_module = _import_module(MODULE_HUBCONF, hubconf_path)

    entry = _load_entry_from_hubconf(hub_module, model)
    model = entry(*args, **kwargs)

    sys.path.remove(hubconf_dir)

    return model


def download_url_to_file(url, dst, hash_prefix=None, progress=True):
    r"""Download object at the given URL to a local path.

    Args:
        url (string): URL of the object to download
        dst (string): Full path where object will be saved, e.g. ``/tmp/temporary_file``
        hash_prefix (string, optional): If not None, the SHA256 downloaded file should start with ``hash_prefix``.
            Default: None
        progress (bool, optional): whether or not to display a progress bar to stderr
            Default: True

    Example:
        >>> torch.hub.download_url_to_file('https://s3.amazonaws.com/pytorch/models/resnet18-5c106cde.pth', '/tmp/temporary_file')

    """
    file_size = None
    req = Request(url, headers={"User-Agent": "torch.hub"})
    u = urlopen(req)
    meta = u.info()
    if hasattr(meta, 'getheaders'):
        content_length = meta.getheaders("Content-Length")
    else:
        content_length = meta.get_all("Content-Length")
    if content_length is not None and len(content_length) > 0:
        file_size = int(content_length[0])

    # We deliberately save it in a temp file and move it after
    # download is complete. This prevents a local working checkpoint
    # being overridden by a broken download.
    dst = os.path.expanduser(dst)
    dst_dir = os.path.dirname(dst)
    f = tempfile.NamedTemporaryFile(delete=False, dir=dst_dir)

    try:
        if hash_prefix is not None:
            sha256 = hashlib.sha256()
        with tqdm(total=file_size, disable=not progress,
                  unit='B', unit_scale=True, unit_divisor=1024) as pbar:
            while True:
                buffer = u.read(8192)
                if len(buffer) == 0:
                    break
                f.write(buffer)
                if hash_prefix is not None:
                    sha256.update(buffer)
                pbar.update(len(buffer))

        f.close()
        if hash_prefix is not None:
            digest = sha256.hexdigest()
            if digest[:len(hash_prefix)] != hash_prefix:
                raise RuntimeError('invalid hash value (expected "{}", got "{}")'
                                   .format(hash_prefix, digest))
        shutil.move(f.name, dst)
    finally:
        f.close()
        if os.path.exists(f.name):
            os.remove(f.name)


def _download_url_to_file(url, dst, hash_prefix=None, progress=True):
    warnings.warn('torch.hub._download_url_to_file has been renamed to\
            torch.hub.download_url_to_file to be a public API,\
            _download_url_to_file will be removed in after 1.3 release')
    download_url_to_file(url, dst, hash_prefix, progress)


# Hub used to support automatically extracts from zipfile manually compressed by users.
# The legacy zip format expects only one file from torch.save() < 1.6 in the zip.
# We should remove this support since zipfile is now default zipfile format for torch.save().
def _is_legacy_zip_format(filename):
    if zipfile.is_zipfile(filename):
        infolist = zipfile.ZipFile(filename).infolist()
        return len(infolist) == 1 and not infolist[0].is_dir()
    return False


def _legacy_zip_load(filename, model_dir, map_location):
    warnings.warn('Falling back to the old format < 1.6. This support will be '
                  'deprecated in favor of default zipfile format introduced in 1.6. '
                  'Please redo torch.save() to save it in the new zipfile format.')
    # Note: extractall() defaults to overwrite file if exists. No need to clean up beforehand.
    #       We deliberately don't handle tarfile here since our legacy serialization format was in tar.
    #       E.g. resnet18-5c106cde.pth which is widely used.
    with zipfile.ZipFile(filename) as f:
        members = f.infolist()
        if len(members) != 1:
            raise RuntimeError('Only one file(not dir) is allowed in the zipfile')
        f.extractall(model_dir)
        extraced_name = members[0].filename
        extracted_file = os.path.join(model_dir, extraced_name)
    return torch.load(extracted_file, map_location=map_location)


def load_state_dict_from_url(url, model_dir=None, map_location=None, progress=True, check_hash=False, file_name=None):
    r"""Loads the Torch serialized object at the given URL.

    If downloaded file is a zip file, it will be automatically
    decompressed.

    If the object is already present in `model_dir`, it's deserialized and
    returned.
    The default value of ``model_dir`` is ``<hub_dir>/checkpoints`` where
    ``hub_dir`` is the directory returned by :func:`~torch.hub.get_dir`.

    Args:
        url (string): URL of the object to download
        model_dir (string, optional): directory in which to save the object
        map_location (optional): a function or a dict specifying how to remap storage locations (see torch.load)
        progress (bool, optional): whether or not to display a progress bar to stderr.
            Default: True
        check_hash(bool, optional): If True, the filename part of the URL should follow the naming convention
            ``filename-<sha256>.ext`` where ``<sha256>`` is the first eight or more
            digits of the SHA256 hash of the contents of the file. The hash is used to
            ensure unique names and to verify the contents of the file.
            Default: False
        file_name (string, optional): name for the downloaded file. Filename from ``url`` will be used if not set.

    Example:
        >>> state_dict = torch.hub.load_state_dict_from_url('https://s3.amazonaws.com/pytorch/models/resnet18-5c106cde.pth')

    """
    # Issue warning to move data if old env is set
    if os.getenv('TORCH_MODEL_ZOO'):
        warnings.warn('TORCH_MODEL_ZOO is deprecated, please use env TORCH_HOME instead')

    if model_dir is None:
        hub_dir = get_dir()
        model_dir = os.path.join(hub_dir, 'checkpoints')

    try:
        os.makedirs(model_dir)
    except OSError as e:
        if e.errno == errno.EEXIST:
            # Directory already exists, ignore.
            pass
        else:
            # Unexpected OSError, re-raise.
            raise

    parts = urlparse(url)
    filename = os.path.basename(parts.path)
    if file_name is not None:
        filename = file_name
    cached_file = os.path.join(model_dir, filename)
    if not os.path.exists(cached_file):
        sys.stderr.write('Downloading: "{}" to {}\n'.format(url, cached_file))
        hash_prefix = None
        if check_hash:
            r = HASH_REGEX.search(filename)  # r is Optional[Match[str]]
            hash_prefix = r.group(1) if r else None
        download_url_to_file(url, cached_file, hash_prefix, progress=progress)

    if _is_legacy_zip_format(cached_file):
        return _legacy_zip_load(cached_file, model_dir, map_location)
    return torch.load(cached_file, map_location=map_location)
