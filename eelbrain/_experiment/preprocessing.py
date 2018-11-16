# Author: Christian Brodbeck <christianbrodbeck@nyu.edu>
"""Pre-processing operations based on NDVars"""
import fnmatch
from os import mkdir, remove
from os.path import dirname, exists, getmtime, join, splitext

import mne
from scipy import signal

from .. import load
from .._data_obj import NDVar
from .._exceptions import DefinitionError
from .._io.fiff import KIT_NEIGHBORS
from .._ndvar import filter_data
from .._utils import ask
from ..mne_fixes import CaptureLog
from .definitions import typed_arg
from .exceptions import FileMissing


class RawPipe(object):

    def _link(self, name, pipes, root, raw_dir, cache_dir, log):
        raise NotImplementedError

    def _link_base(self, name, path, root, log):
        self.name = name
        self.path = path
        self.root = root
        self.log = log

    def as_dict(self):
        return {'type': self.__class__.__name__, 'name': self.name}

    def cache(self, subject, session):
        "Make sure the file exists and is up to date"
        raise NotImplementedError

    def get_connectivity(self, data):
        raise NotImplementedError

    def get_sysname(self, info, subject, data):
        raise NotImplementedError

    def load(self, subject, session, add_bads=True, preload=False, raw=None):
        # raw
        if raw is None:
            raw = self._load(subject, session, preload)
        # bad channels
        if add_bads:
            raw.info['bads'] = self.load_bad_channels(subject, session)
        else:
            raw.info['bads'] = []
        return raw

    def _load(self, subject, session, preload):
        path = self.path.format(root=self.root, subject=subject, session=session)
        return mne.io.read_raw_fif(path, preload=preload)

    def load_bad_channels(self, subject, session):
        raise NotImplementedError

    def make_bad_channels(self, subject, session, bad_chs, redo):
        raise NotImplementedError

    def make_bad_channels_auto(self, subject, session, flat):
        raise NotImplementedError

    def mtime(self, subject, session, bad_chs=True):
        "Modification time of anything influencing the output of load"
        raise NotImplementedError


class RawSource(RawPipe):
    """Raw data source

    Parameters
    ----------
    filename : str
        Pattern for filename (default ``'{subject}_{session}-raw.fif'``).
    reader : callable
        Function for reading data (default is :func:`mne.io.read_raw_fif`).
    sysname : str
        Used to determine sensor positions (not needed for KIT files, or when a
        montage is specified).
    rename_channels : dict
        Rename channels before applying montage, ``{from: to}`` dictionary;
        useful to convert idiosyncratic naming conventions to standard montages.
    montage : str
        Name of a montage that is applied to raw data to set sensor positions.
    connectivity : str | list of (str, str)
        Connectivity between sensors. Can be specified as:

        - list of connections (e.g., ``[('OZ', 'O1'), ('OZ', 'O2'), ...]``)
        - :class:`numpy.ndarray` of int, shape (n_edges, 2), to specify
          connections in terms of indices. Each row should specify one
          connection [i, j] with i < j. If the array's dtype is uint32,
          property checks are disabled to improve efficiency.
        - ``"grid"`` to use adjacency in the sensor names

        If unspecified, it is inferred from ``sysname`` if possible.
    ...
        Additional parameters for the ``reader`` function.
    """
    _dig_sessions = None

    def __init__(self, filename='{subject}_{session}-raw.fif', reader=mne.io.read_raw_fif, sysname=None, rename_channels=None, montage=None, connectivity=None, **kwargs):
        RawPipe.__init__(self)
        self.filename = typed_arg(filename, str)
        self.reader = reader
        self.sysname = sysname
        self.rename_channels = typed_arg(rename_channels, dict)
        self.montage = typed_arg(montage, str)
        self.connectivity = connectivity
        self._kwargs = kwargs
        if reader is mne.io.read_raw_cnt:
            self._read_raw_kwargs = {'montage': None, **kwargs}
        else:
            self._read_raw_kwargs = kwargs

    def _link(self, name, pipes, root, raw_dir, cache_dir, log):
        if name != 'raw':
            raise NotImplementedError("RawSource with name {name!r}: the raw source must be called 'raw'")
        path = join(raw_dir, self.filename)
        if self.filename.endswith('-raw.fif'):
            head = path[:-8]
        else:
            head = splitext(path)[0]
        self.bads_path = head + '-bad_channels.txt'
        RawPipe._link_base(self, name, path, root, log)

    def as_dict(self):
        out = RawPipe.as_dict(self)
        out.update(self._kwargs)
        if self.reader != mne.io.read_raw_fif:
            out['reader'] = self.reader.__name__
        if self.rename_channels:
            out['rename_channels'] = self.rename_channels
        if self.montage:
            out['montage'] = self.montage
        if self.connectivity is not None:
            out['connectivity'] = self.connectivity
        return out
    
    def _load(self, subject, session, preload):
        path = self.path.format(root=self.root, subject=subject, session=session)
        raw = self.reader(path, preload=preload, **self._read_raw_kwargs)
        if self.rename_channels:
            raw.rename_channels(self.rename_channels)
        if self.montage:
            raw.set_montage(self.montage)
        if raw.info['dig'] is None:
            dig_session = self._dig_sessions[subject][session]
            dig_raw = self._load(subject, dig_session, False)
            raw.info['dig'] = dig_raw.info['dig']
        return raw

    def cache(self, subject, session):
        "Make sure the file exists and is up to date"
        path = self.path.format(root=self.root, subject=subject, session=session)
        if not exists(path):
            raise FileMissing(f"Raw input file for {subject}/{session} does not exist at expected location {path}")
        return path

    def get_connectivity(self, data):
        if data == 'eog':
            return None
        else:
            return self.connectivity

    def get_sysname(self, info, subject, data):
        if data == 'eog':
            return None
        elif data == 'mag':
            kit_system_id = info.get('kit_system_id')
            if kit_system_id:
                try:
                    return KIT_NEIGHBORS[kit_system_id]
                except KeyError:
                    raise NotImplementedError(f"Unknown KIT system-ID: {kit_system_id}; please contact developers")
        if isinstance(self.sysname, str):
            return self.sysname
        elif isinstance(self.sysname, dict):
            for k, v in self.sysname.items():
                if fnmatch.fnmatch(subject, k):
                    return v
        elif self.connectivity is None:
            raise RuntimeError(f"Unknown sensor configuration for {subject}, data={data!r}. Consider setting connectivity or sysname explicitly.")

    def load_bad_channels(self, subject, session):
        path = self.bads_path.format(root=self.root, subject=subject, session=session)
        if not exists(path):
            # need to create one to know mtime after user deletes the file
            self.log.info("Generating bad_channels file for %s %s",
                          subject, session)
            self.make_bad_channels_auto(subject, session)
        with open(path) as fid:
            return [l for l in fid.read().splitlines() if l]

    def make_bad_channels(self, subject, session, bad_chs, redo):
        path = self.bads_path.format(root=self.root, subject=subject, session=session)
        if exists(path):
            old_bads = self.load_bad_channels(subject, session)
        else:
            old_bads = None
        # find new bad channels
        if isinstance(bad_chs, (str, int)):
            bad_chs = (bad_chs,)
        raw = self.load(subject, session, add_bads=False)
        sensor = load.fiff.sensor_dim(raw)
        new_bads = sensor._normalize_sensor_names(bad_chs)
        # update with old bad channels
        if old_bads is not None and not redo:
            new_bads = sorted(set(old_bads).union(new_bads))
        # print change
        print(f"{old_bads} -> {new_bads}")
        # write new bad channels
        text = '\n'.join(new_bads)
        with open(path, 'w') as fid:
            fid.write(text)

    def make_bad_channels_auto(self, subject, session, flat=1e-14, redo=False):
        if not flat:
            return
        raw = self.load(subject, session, preload=True, add_bads=False)
        raw = load.fiff.raw_ndvar(raw)
        bad_chs = raw.sensor.names[raw.std('time') < flat]
        self.make_bad_channels(subject, session, bad_chs, redo)

    def mtime(self, subject, session, bad_chs=True):
        path = self.path.format(root=self.root, subject=subject, session=session)
        if exists(path):
            mtime = getmtime(path)
            if not bad_chs:
                return mtime
            path = self.bads_path.format(root=self.root, subject=subject, session=session)
            if exists(path):
                return max(mtime, getmtime(path))


class CachedRawPipe(RawPipe):

    _bad_chs_affect_cache = False

    def __init__(self, source, cache=True):
        RawPipe.__init__(self)
        self._source_name = source
        self._cache = cache

    def _link(self, name, pipes, root, raw_dir, cache_path, log):
        path = cache_path.format(root='{root}', raw=name, subject='{subject}', session='{session}')
        if self._source_name not in pipes:
            raise DefinitionError(f"{self.__class__.__name__} {name!r} source {self._source_name!r} does not exist")
        self.source = pipes[self._source_name]
        RawPipe._link_base(self, name, path, root, log)

    def as_dict(self):
        out = RawPipe.as_dict(self)
        out['source'] = self._source_name
        return out

    def cache(self, subject, session):
        "Make sure the cache is up to date"
        path = self.path.format(root=self.root, subject=subject, session=session)
        if (not exists(path) or getmtime(path) <
                self.mtime(subject, session, self._bad_chs_affect_cache)):
            from .. import __version__
            # make sure directory exists
            dir_path = dirname(path)
            if not exists(dir_path):
                mkdir(dir_path)
            # generate new raw
            with CaptureLog(path[:-3] + 'log') as logger:
                logger.info(f"eelbrain {__version__}")
                logger.info(f"mne {mne.__version__}")
                logger.info(repr(self.as_dict()))
                raw = self._make(subject, session)
            # save
            raw.save(path, overwrite=True)
            return raw

    def get_connectivity(self, data):
        return self.source.get_connectivity(data)

    def get_sysname(self, info, subject, data):
        return self.source.get_sysname(info, subject, data)

    def load(self, subject, session, add_bads=True, preload=False, raw=None):
        if raw is not None:
            pass
        elif self._cache:
            raw = self.cache(subject, session)
        else:
            raw = self._make(subject, session)
        if not isinstance(raw, mne.io.Raw):
            raw = None  # only propagate fiff raw for appending
        return RawPipe.load(self, subject, session, add_bads, preload, raw)

    def load_bad_channels(self, subject, session):
        return self.source.load_bad_channels(subject, session)

    def _make(self, subject, session):
        raise NotImplementedError

    def make_bad_channels(self, subject, session, bad_chs, redo):
        self.source.make_bad_channels(subject, session, bad_chs, redo)

    def make_bad_channels_auto(self, *args, **kwargs):
        self.source.make_bad_channels_auto(*args, **kwargs)

    def mtime(self, subject, session, bad_chs=True):
        return self.source.mtime(subject, session, bad_chs)


class RawFilter(CachedRawPipe):

    def __init__(self, source, l_freq=None, h_freq=None, **kwargs):
        CachedRawPipe.__init__(self, source)
        self.args = (l_freq, h_freq)
        self.kwargs = kwargs
        # mne backwards compatibility (fir_design default change 0.15 -> 0.16)
        if kwargs.get('fir_design', None) is not None:
            self._use_kwargs = kwargs
        else:
            self._use_kwargs = kwargs.copy()
            self._use_kwargs['fir_design'] = 'firwin2'

    def as_dict(self):
        out = CachedRawPipe.as_dict(self)
        out['args'] = self.args
        out['kwargs'] = self.kwargs
        return out

    def filter_ndvar(self, ndvar):
        return filter_data(ndvar, *self.args, **self._use_kwargs)

    def _make(self, subject, session):
        raw = self.source.load(subject, session, preload=True)
        self.log.debug("Raw %s: filtering for %s/%s...", self.name, subject, session)
        raw.filter(*self.args, **self._use_kwargs)
        return raw


class RawFilterElliptic(CachedRawPipe):

    def __init__(self, source, low_stop, low_pass, high_pass, high_stop, gpass, gstop):
        CachedRawPipe.__init__(self, source)
        self.args = (low_stop, low_pass, high_pass, high_stop, gpass, gstop)

    def as_dict(self):
        out = CachedRawPipe.as_dict(self)
        out['args'] = self.args
        return out

    def _sos(self, sfreq):
        nyq = sfreq / 2.
        low_stop, low_pass, high_pass, high_stop, gpass, gstop = self.args
        if high_stop is None:
            assert low_stop is not None
            assert high_pass is None
        else:
            high_stop /= nyq
            high_pass /= nyq

        if low_stop is None:
            assert low_pass is None
        else:
            low_pass /= nyq
            low_stop /= nyq

        if low_stop is None:
            btype = 'lowpass'
            wp, ws = high_pass, high_stop
        elif high_stop is None:
            btype = 'highpass'
            wp, ws = low_pass, low_stop
        else:
            btype = 'bandpass'
            wp, ws = (low_pass, high_pass), (low_stop, high_stop)
        order, wn = signal.ellipord(wp, ws, gpass, gstop)
        return signal.ellip(order, gpass, gstop, wn, btype, output='sos')

    def filter_ndvar(self, ndvar):
        axis = ndvar.get_axis('time')
        sos = self._sos(1. / ndvar.time.tstep)
        x = signal.sosfilt(sos, ndvar.x, axis)
        return NDVar(x, ndvar.dims, ndvar.info.copy(), ndvar.name)

    def _make(self, subject, session):
        raw = self.source.load(subject, session, preload=True)
        self.log.debug("Raw %s: filtering for %s/%s...", self.name, subject,
                       session)
        # filter data
        picks = mne.pick_types(raw.info, eeg=True, ref_meg=True)
        sos = self._sos(raw.info['sfreq'])
        for i in picks:
            raw._data[i] = signal.sosfilt(sos, raw._data[i])
        # update info
        low, high = self.args[1], self.args[2]
        if high and raw.info['lowpass'] > high:
            raw.info['lowpass'] = float(high)
        if low and raw.info['highpass'] < low:
            raw.info['highpass'] = float(low)
        return raw


class RawICA(CachedRawPipe):
    """ICA raw pipe

    Parameters
    ----------
    source : str
        Name of the raw pipe to use for input data.
    session : str | sequence of str
        Session(s) to use as input data for ICA.
    ...
        Parameters for :class:`mne.preprocessing.ICA`.

    Notes
    -----
    This pipe merges bad channels from its source raw pipes.

    When checking whether the ICA file is up to date, the ICA does not check
    raw source mtime. However, if bad channels change the ICA is automatically
    recomputed.
    """

    def __init__(self, source, session, **kwargs):
        CachedRawPipe.__init__(self, source)
        if isinstance(session, str):
            session = (session,)
        else:
            if not isinstance(session, tuple):
                session = tuple(session)
            assert all(isinstance(s, str) for s in session)
        self.session = session
        self.kwargs = kwargs

    def _link(self, name, pipes, root, raw_dir, cache_path, log):
        CachedRawPipe._link(self, name, pipes, root, raw_dir, cache_path, log)
        self.ica_path = join(raw_dir, f'{{subject}} {name}-ica.fif')

    def as_dict(self):
        out = CachedRawPipe.as_dict(self)
        out['session'] = self.session
        out['kwargs'] = self.kwargs
        return out

    def load_bad_channels(self, subject, session=None):
        bad_chs = set()
        for session in self.session:
            bad_chs.update(self.source.load_bad_channels(subject, session))
        return sorted(bad_chs)

    def load_ica(self, subject):
        path = self.ica_path.format(root=self.root, subject=subject)
        if not exists(path):
            raise RuntimeError("ICA file does not exist for raw=%r, "
                               "subject=%r. Run e.make_ica_selection() to "
                               "create it." % (self.name, subject))
        return mne.preprocessing.read_ica(path)

    @staticmethod
    def _check_ica_channels(ica, raw):
        picks = mne.pick_types(raw.info, eeg=True, ref_meg=False)
        return ica.ch_names == [raw.ch_names[i] for i in picks]

    def make_ica(self, subject):
        path = self.ica_path.format(root=self.root, subject=subject)
        raw = self.source.load(subject, self.session[0], False)
        bad_channels = self.load_bad_channels(subject)
        raw.info['bads'] = bad_channels
        if exists(path):
            ica = mne.preprocessing.read_ica(path)
            if self._check_ica_channels(ica, raw):
                return path
            self.log.info("Raw %s: ICA outdated due to change in bad channels for %s", self.name, subject)

        for session in self.session[1:]:
            raw_ = self.source.load(subject, session, False)
            raw_.info['bads'] = bad_channels
            raw.append(raw_)

        self.log.debug("Raw %s: computing ICA decomposition for %s", self.name, subject)
        ica = mne.preprocessing.ICA(max_iter=256, **self.kwargs)
        # reject presets from meeg-preprocessing
        ica.fit(raw, reject={'mag': 5e-12, 'grad': 5000e-13, 'eeg': 300e-6})
        ica.save(path)
        return path

    def _make(self, subject, session):
        raw = self.source.load(subject, session, preload=True)
        raw.info['bads'] = self.load_bad_channels(subject)
        ica = self.load_ica(subject)
        if not self._check_ica_channels(ica, raw):
            raise RuntimeError(f"Raw {self.name}, ICA for {subject} outdated due to change in bad channels. Reset bad channels or re-run .make_ica().")
        self.log.debug("Raw %s: applying ICA for %s/%s...", self.name, subject, session)
        ica.apply(raw)
        return raw

    def mtime(self, subject, session, bad_chs=True):
        mtime = CachedRawPipe.mtime(self, subject, session, bad_chs)
        if mtime:
            path = self.ica_path.format(root=self.root, subject=subject)
            if exists(path):
                return max(mtime, getmtime(path))


class RawMaxwell(CachedRawPipe):
    "Maxwell filter raw pipe"

    _bad_chs_affect_cache = True

    def __init__(self, source, **kwargs):
        CachedRawPipe.__init__(self, source)
        self.kwargs = kwargs

    def as_dict(self):
        out = CachedRawPipe.as_dict(self)
        out['kwargs'] = self.kwargs
        return out

    def _make(self, subject, session):
        raw = self.source.load(subject, session)
        self.log.debug("Raw %s: computing Maxwell filter for %s/%s", self.name, subject, session)
        return mne.preprocessing.maxwell_filter(raw, **self.kwargs)


class RawReReference(CachedRawPipe):

    def __init__(self, source, reference='average'):
        CachedRawPipe.__init__(self, source, False)
        if not isinstance(reference, str):
            reference = list(reference)
            if not all(isinstance(ch, str) for ch in reference):
                raise TypeError(f"reference={reference}: must be list of str")
        self.reference = reference

    def as_dict(self):
        out = CachedRawPipe.as_dict(self)
        out['reference'] = self.reference
        return out

    def _make(self, subject, session):
        raw = self.source.load(subject, session, preload=True)
        raw.set_eeg_reference(self.reference)
        return raw


def assemble_pipeline(raw_dict, raw_dir, cache_path, root, sessions, log):
    "Assemble preprocessing pipeline form a definition in a dict"
    # convert to Raw objects
    raw = {}
    for key, raw_def in raw_dict.items():
        if not isinstance(raw_def, RawPipe):
            params = {**raw_def}
            source = params.pop('source', None)
            if source is None:
                raw_def = RawSource(**params)
            else:
                pipe_type = params.pop('type')
                if pipe_type == 'filter':
                    raw_def = RawFilter(source, *params.pop('args', ()), **params.pop('kwargs', {}))
                elif pipe_type == 'ica':
                    raw_def = RawICA(source, params.pop('session'), **params.pop('kwargs', {}))
                elif pipe_type == 'maxwell_filter':
                    raw_def = RawMaxwell(source, **params.pop('kwargs', {}))
                else:
                    raise DefinitionError(f"Raw {key!r}: unknonw type {pipe_type!r}")
                if params:
                    raise DefinitionError(f"Unused parameters in raw definition {key!r}: {raw_def}")
        raw[key] = raw_def
    n_source = sum(isinstance(p, RawSource) for p in raw.values())
    if n_source == 0:
        raise DefinitionError("No RawSource pipe")
    elif n_source > 1:
        raise NotImplementedError("More than one RawSource pipes")
    # link sources
    for key, pipe in raw.items():
        pipe._link(key, raw, root, raw_dir, cache_path, log)
        if isinstance(pipe, RawICA):
            missing = set(pipe.session).difference(sessions)
            if missing:
                raise DefinitionError(f"RawICA {key!r} lists one or more non-exising sessions: {', '.join(missing)}")
    # check tree
    is_ok = ['raw']
    for key, pipe in raw.items():
        tested = []
        name = key
        while True:
            if name in is_ok:
                is_ok.append(key)
                break
            elif name in tested:
                raise DefinitionError(f"Unable to resolve source for {name!r} preprocessing pipeline, circular dependency?")
            tested.append(name)
            name = raw[name]._source_name
    return raw


###############################################################################
# Comparing pipelines
######################


def pipeline_dict(pipeline):
    return {k: v.as_dict() for k, v in pipeline.items()}


def compare_pipelines(old, new, log):
    """Return a tuple of raw keys for which definitions changed

    Parameters
    ----------
    old : {str: dict}
        A {name: params} dict for the previous preprocessing pipeline.
    new : {str: dict}
        Current pipeline.
    log : logger
        Logger for logging changes.

    Returns
    -------
    bad_raw : {str: str}
        ``{pipe_name: status}`` dictionary. Status can be 'new', 'removed' or
        'changed'.
    bad_ica : {str: str}
        Same as ``bad_raw`` but only for RawICA pipes (for which ICA files
        might have to be removed).
    """
    out = {k: 'new' for k in new if k not in old}
    out.update({k: 'removed' for k in old if k not in new})

    # parameter changes
    to_check = set(new) - set(out)
    for key in tuple(to_check):
        if new[key] != old[key]:
            log.debug("  raw changed: %s %s -> %s", key, old[key], new[key])
            out[key] = 'changed'
            to_check.remove(key)

    # does not need to be checked for source
    if 'raw' in to_check:
        to_check.remove('raw')
        out['raw'] = 'good'

    # secondary changes
    while to_check:
        n = len(to_check)
        for key in tuple(to_check):
            parent = new[key]['source']
            if parent in out:
                out[key] = out[parent]
                to_check.remove(key)
        if len(to_check) == n:
            raise RuntimeError("Queue not decreasing")

    bad_raw = {k: v for k, v in out.items() if v != 'good'}
    bad_ica = {k: v for k, v in bad_raw.items() if
               new.get(k, old.get(k))['type'] == 'RawICA'}
    return bad_raw, bad_ica


def ask_to_delete_ica_files(raw, status, filenames):
    "Ask whether outdated ICA files should be removed and act accordingly"
    if status == 'new':
        msg = ("The definition for raw=%r has been added, but ICA-files "
               "already exist. These files might not correspond to the new "
               "settings and should probably be deleted." % (raw,))
    elif status == 'removed':
        msg = ("The definition for raw=%r has been removed. The corresponsing "
               "ICA files should probably be deleted:" % (raw,))
    elif status == 'changed':
        msg = ("The definition for raw=%r has changed. The corresponding ICA "
               "files should probably be deleted." % (raw,))
    else:
        raise RuntimeError("status=%r" % (status,))
    command = ask(
        "%s Delete %i files?" % (msg, len(filenames)),
        (('abort', 'abort to fix the raw definition and try again'),
         ('delete', 'delete the invalid fils'),
         ('ignore', 'pretend that the files are valid; you will not be warned again')))

    if command == 'delete':
        for filename in filenames:
            remove(filename)
    elif command == 'abort':
        raise RuntimeError("User abort")
    elif command != 'ignore':
        raise RuntimeError("command=%r" % (command,))
