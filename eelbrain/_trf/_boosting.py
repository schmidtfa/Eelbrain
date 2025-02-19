"""Boosting as described by David et al. (2007).

Versions
--------
Stored in ``algorithm_version`` attribute (see docstring)

Profiling
---------
ds = datasets._get_continuous()
y = ds['y']
x1 = ds['x1']
x2 = ds['x2']

%prun -s cumulative res = boosting(y, x1, 0, 1)

"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from functools import cached_property, reduce
import inspect
from itertools import chain, repeat
from math import ceil
from operator import mul
import time
from typing import Any, Callable, List, Literal, Optional, Union, Tuple, Sequence
import warnings

import numba
import numpy as np

from .._config import CONFIG
from .._data_obj import Case, Dataset, Dimension, SourceSpaceBase, NDVar, CategorialArg, NDVarArg, dataobj_repr
from .._exceptions import OldVersionError
from .._ndvar import _concatenate_values, convolve_jit, parallel_convolve, set_connectivity, set_parc
from .._utils import PickleableDataClass, user_activity
from .shared import PredictorData, DeconvolutionData, Split, Splits, merge_segments
from ._fit_metrics import get_evaluators
from . import _boosting_opt as opt


@dataclass(eq=False)
class BoostingResult(PickleableDataClass):
    """Result from boosting

    Fit metrics are computed from all time points in ``y``.

    - For models estimated with ``test=False``, the entire ``y_pred`` is
      predicted with the averaged TRF from the different runs (with each run
      corresponding to one validation set).
    - For models estimated with ``test=True``, ``y_pred`` in each test segment
      is predicted from the averaged TRF from the corresponding training runs
      (each non-test segment used as validation set once), and the different
      test segments are then concatenated to compute the fit-metrics in
      comparison with ``y``.


    Attributes
    ----------
    h : NDVar | tuple of NDVar
        The temporal response function (the average of all TRFs from the
        different runs/partitions).
        Whether ``h`` is an :class:`NDVar` or a :class:`tuple` of :class:`NDVar`
        depends on whether the ``x`` parameter to :func:`boosting` was an
        :class:`NDVar` or a sequence of :class:`NDVar`.
    h_scaled : NDVar | tuple of NDVar
        ``h`` scaled such that it applies to the original input ``y`` and ``x``.
        If boosting was done with ``scale_data=False``, ``h_scaled`` is the same
        as ``h``.
    h_source : NDVar | tuple of NDVar
        If ``h`` was constructed using a basis, ``h_source`` represents the
        source of ``h`` before being convolved with the basis.
    h_time : UTS
        Time dimension of the kernel.
    r : float | NDVar
        Correlation between the measured response ``y`` and the predicted
        response ``h * x``. When using cross-validation (calling
        :func:`boosting` with ``test=True``), each partition of ``y`` is
        predicted using the ``h`` estimated from the corresponding training
        partitions. Otherwise, all of ``y`` is estimated using the average ``h``.
        For vector data, measured and predicted responses are normalized, and ``r``
        is computed as the average dot product over time.
        The type of ``r`` depends on the ``y`` parameter to :func:`boosting`:
        If ``y`` is one-dimensional, ``r`` is scalar, otherwise it is a :class:`NDVar`.
        Note that ``r`` does not take into account the model's ability to predict
        the magnitude of the response, only its shape; for a measure that reflects both,
        consider using ``proportion_explained``.
    r_rank : float | NDVar
        As ``r``, the Spearman rank correlation.
    t_run : float
        Time it took to run the boosting algorithm (in seconds).
    error : str
        The error evaluation method used.
    residual : float | NDVar
        The residual of the final result

        - ``error='l1'``: the sum of the absolute differences between ``y`` and
          ``h * x``.
        - ``error='l2'``: the sum of the squared differences between ``y`` and
          ``h * x``.

        For vector ``y``, the error is defined based on the distance in space
        for each data point.
    delta : scalar
        Kernel modification step used.
    mindelta : None | scalar
        Mindelta parameter used.
    n_samples : int
        Number of samples in the input data time axis.
    proportion_explained : float | NDVar
        The proportion of the variation in ``y`` that is explained by the model.
        Calculated as ``1 - (variation(residual) / variation(y))``.
        Variation is caculated as the ``l1`` or ``l2`` norm,
        depending on the ``error`` that was used for model fitting.
        Note that this does not correspond to ``r**2`` even for ``error='l2'``,
        because residuals are not guaranteed to be orthogonal to predictions.
    scale_data : bool
        Scale_data parameter used.
    y_mean : NDVar | scalar
        Mean that was subtracted from ``y``.
    y_scale : NDVar | scalar
        Scale by which ``y`` was divided.
    x_mean : NDVar | scalar | tuple
        Mean that was subtracted from ``x``.
    x_scale : NDVar | scalar | tuple
        Scale by which ``x`` was divided.
    splits : Splits
        Data splits used for cross-validation.
        Use :meth:`.splits.plot` to visualize the cross-validation scheme.
    partition_results : list of BoostingResuls
        If :func:`boosting` is called with ``partition_results=True``, this
        attribute contains the results for the individual test paritions.
    algorithm_version : int
        Version of the algorithm with which the model was estimated

          - -1: results from before this attribute was added
          - 0: Normalize ``x`` after applying basis
          - 1: Numba implementation
          - 2: Cython multiprocessing implementation (Eelbrain 0.38)
    """
    # basic parameters
    y: Optional[str]
    x: Union[Optional[str], Tuple[Optional[str]]]
    tstart: Union[float, Tuple[float, ...]]
    tstop: Union[float, Tuple[float, ...]]
    scale_data: bool
    delta: float
    mindelta: float
    error: str
    selective_stopping: int
    # data properties
    y_mean: NDVar
    y_scale: NDVar
    x_mean: Union[NDVar, Tuple[NDVar, ...]]
    x_scale: Union[NDVar, Tuple[NDVar, ...]]
    # results
    _h: Union[NDVar, Tuple[NDVar, ...]]
    _isnan: np.ndarray
    t_run: float
    # advanced parameters
    basis: float
    basis_window: str
    splits: Splits = None
    # advanced data properties
    n_samples: int = None
    _y_info: dict = field(default_factory=dict)
    _y_dims: Tuple[Dimension, ...] = None
    # fit metrics
    i_test: int = None  # test partition for fit metrics
    residual: Union[float, NDVar] = None
    r: Union[float, NDVar] = None
    r_rank: Union[float, NDVar] = None
    r_l1: NDVar = None
    partition_results: List[BoostingResult] = None
    # store the version of the boosting algorithm with which model was fit
    version: int = 13  # file format (updates when re-saving)
    algorithm_version: int = -1  # does not change when re'saving
    # debug parameters
    y_pred: NDVar = None
    fit: Any = None  # scanpydoc can't handle undocumented 'Boosting'
    # legacy attributes
    prefit: str = None

    def __post_init__(self):
        if self.splits is None:
            self.partitions = None
        else:
            self.partitions = self.splits.n_partitions

    def __setstate__(self, state: dict):
        # backwards compatibility
        version = state.pop('version')
        if version < self.version:
            if version == 7:
                state['partitions'] = state.pop('n_partitions')
                state['partitions_arg'] = state.pop('n_partitions_arg')
            if version < 9:
                state['residual'] = state.pop('fit_error')
            if version < 11:
                for key in ['partitions_arg', 'h', 'isnan', 'y_info']:
                    state[f'_{key}'] = state.pop(key, None)
                state['r_rank'] = state.pop('spearmanr')
            if version < 12:
                # Vector residuals are averaged
                if state['r_rank'] is None:
                    state['residual'] *= state['n_samples']
            if version < 13:
                state['splits'] = Splits(None, state.pop('_partitions_arg'), state.pop('partitions'), state.pop('validate', 1), state.pop('test', 0), state.pop('model'))
        PickleableDataClass.__setstate__(self, state)

    def __repr__(self):
        items = []
        if isinstance(self.tstart, tuple):
            x = ' + '.join(f'{x} ({t1:g} - {t2:g})' for x, t1, t2 in zip(self.x, self.tstart, self.tstop))
        else:
            if self.x is None or isinstance(self.x, str):
                x = self.x
            else:
                x = ' + '.join(map(str, self.x))
            items.append(f'{self.tstart:g} - {self.tstop:g}')
        items.insert(0, f'boosting {self.y} ~ {x}')
        for name, param in inspect.signature(boosting).parameters.items():
            if param.default is inspect.Signature.empty or name == 'ds':
                continue
            elif name == 'debug':
                continue
            elif name == 'partition_results':
                value = bool(self.partition_results)
            elif name == 'partitions':
                value = None if self.splits is None else self.splits.partitions_arg
            elif name == 'model':
                if self.splits is None or self.splits.model is None:
                    continue
                value = dataobj_repr(self.splits.model)
            elif name == 'validate':
                value = None if self.splits is None else self.splits.n_validate
            elif name == 'test':
                value = None if self.splits is None else self.splits.n_test
            else:
                value = getattr(self, name)
            if value is not None and value != param.default:
                items.append(f'{name}={value}')
        return f"<{', '.join(items)}>"

    @cached_property
    def h(self):
        if not self.basis:
            return self._h
        elif isinstance(self._h, tuple):
            return tuple(h.smooth('time', self.basis, self.basis_window, 'full') for h in self._h)
        else:
            return self._h.smooth('time', self.basis, self.basis_window, 'full')

    @cached_property
    def h_scaled(self):
        if self.y_scale is None:
            return self.h
        elif isinstance(self.h, NDVar):
            out = self.h * (self.y_scale / self.x_scale)
            out.info = self._y_info.copy()
            return out
        else:
            out = []
            for h, sx in zip(self.h, self.x_scale):
                h = h * (self.y_scale / sx)
                h.info = self._y_info.copy()
                out.append(h)
            return tuple(out)

    @cached_property
    def h_source(self):
        return self._h

    @cached_property
    def h_time(self):
        if isinstance(self.h, NDVar):
            return self.h.time
        else:
            return self.h[0].time

    @cached_property
    def _variability(self):
        # variability in the data
        if self.y_scale is None:
            raise NotImplementedError("Not implemented for scale_data=False")
        elif self.n_samples is None:
            raise OldVersionError("This is an older result object which did not store some necessary information; refit the model to use this attribute")
        else:
            # Due to the normalization:
            return self.n_samples

    def cross_predict(
            self,
            x: Union[NDVarArg, Sequence[NDVarArg]] = None,
            ds: Dataset = None,
            name: str = None,
    ) -> NDVar:
        """Predict responses to ``x`` using complementary training data

        Parameters
        ----------
        x
            Predictors used in the original model fit, or a subset thereof.
            In order for cross-prediction to be accurate, ``x`` needs to match the ``x``
            used in the original fit exactly in cases and time.
        ds
            Dataset with predictors. If ``ds`` is specified, ``x`` can be omitted.
        name
            Name for the output :class:`NDVar`.

        See Also
        --------
        convolve : Simple prediction of linear model

        Notes
        -----
        This function does not adjust the mean across time of predicted
        responses; subtract the mean in order to compute explained variance.
        """
        # predictors
        x_ = self.x if x is None else x
        x_data = PredictorData(x_, ds)
        if not self.partition_results:
            raise ValueError("BoostingResult does not contain partition-specific models; fit with partition_results=True")
        # check predictors match h
        if x_data.x_name == self.x:
            x_use = None
        else:
            if isinstance(self.x, str):
                raise ValueError(f'{x=} for {self}')
            elif x_data.multiple_x:
                x_use = x_data.x_name
            else:
                x_use = [x_data.x_name]
            missing = set(x_use).difference(self.x)
            if missing:
                raise ValueError(f"{x=}: has variables not in {self}:\nx: {', '.join(missing)}")
        # prepare output array
        if self._y_dims is None:  # only possible in results from dev version
            y_dims = self.y_mean.dims
        else:
            y_dims = self._y_dims
        y_dimnames = [dim.name for dim in y_dims]
        n_y = sum(len(dim) for dim in y_dims) or 1
        y_pred = np.zeros((1, n_y, x_data.n_times_flat))
        # prepare x:  (n_x_only, n_shared, n_x_times)
        x_array = x_data.data[np.newaxis, :, :]
        # prepare h
        h_i_start = int(round(self.h_time.tmin / self.h_time.tstep))
        h_i_stop = h_i_start + len(self.h_time)
        # iterate through partitions
        for result in self.partition_results:
            # find segments
            for split in self.splits.splits:
                if split.i_test == result.i_test:
                    segments = split.test
                    break
            else:
                raise RuntimeError(f"Split missing for test segment {result.i_test}")
            # h to flat array: (n_h_only == in y, n_shared == in x, n_h_times)
            hs = [result.h_scaled] if isinstance(result.h_scaled, NDVar) else result.h_scaled
            if x_use:
                hs = {h.name: h for h in hs}
                hs = [hs[x] for x in x_use]
            h_array = []
            for h, (name, xdims, index) in zip(hs, x_data.x_meta):
                dimnames = [*y_dimnames, *[dim.name for dim in xdims], 'time']
                h_data = h.get_data(dimnames)
                h_data = h_data.reshape((n_y, -1, h_data.shape[-1]))
                h_array.append(h_data)
            h_array = np.concatenate(h_array, 1)
            # convolution
            for start, stop in segments:
                parallel_convolve(h_array, x_array[:, :, start:stop], y_pred[:, :, start:stop], h_i_start, h_i_stop)
        # package output
        dims = [*y_dims, x_data.time_dim]
        shape = [len(dim) for dim in dims]
        if x_data.case_to_segments:
            dims = (Case, *dims)
            shape.insert(-1, x_data.n_cases)
        y_pred = y_pred.reshape(shape)
        if x_data.case_to_segments:
            y_pred = np.rollaxis(y_pred, -2)
        if name is None:
            name = self.y
        return NDVar(y_pred, dims, name, self._y_info)

    def partition_result_data(self) -> Dataset:
        """Results from the different test partitions in a :class:`Dataset`"""
        h_is_list = isinstance(self._h, tuple)
        rows = []
        for res in self.partition_results:
            hs = res.h if h_is_list else [res.h]
            rows.append([res.i_test, res.r, res.proportion_explained, *hs])
        if self.x in (None, (None,)):
            xs = ['x']
        elif isinstance(self.x, str):
            xs = [self.x]
        else:
            xs = [f'x_{i}' if x is None else x for i, x in enumerate(self.x)]
        return Dataset.from_caselist(['i_test', 'r', 'det', *xs], rows)

    @cached_property
    def proportion_explained(self):
        return 1 - (self.residual / self._variability)

    def _apply_ndvar_transform(self, func: Callable):
        "Apply func to all NDVars in-place (only for source space transformation)"
        def sub_func(obj):
            if obj is None:
                return None
            elif isinstance(obj, tuple):
                return tuple(sub_func(obj_) for obj_ in obj)
            return func(obj)

        # NDVars
        for attr in ('_h', 'r', 'r_rank', 'residual', 'y_mean', 'y_scale'):
            setattr(self, attr, sub_func(getattr(self, attr)))

        # List of Dimension
        if self._y_dims is not None:
            self._y_dims = tuple([sub_func(dim) if isinstance(dim, SourceSpaceBase) else dim for dim in self._y_dims])

        if self.partition_results:
            for res in self.partition_results:
                res._apply_ndvar_transform(func)

    def _morph(self, to_subject: str):
        "Morph source space"
        from .._mne import morph_source_space

        def func(obj: NDVar):
            return morph_source_space(obj, to_subject)
        self._apply_ndvar_transform(func)

    def _set_connectivity(self, dim, connectivity):
        def func(obj: NDVar):
            return set_connectivity(obj, dim, connectivity)
        self._apply_ndvar_transform(func)

    def _set_parc(self, parc: str):
        """Change the parcellation of source-space result

        Notes
        -----
        No warning for missing sources!
        """
        def func(obj: NDVar):
            return set_parc(obj, parc, mask=True)
        self._apply_ndvar_transform(func)

    @classmethod
    def _eelbrain_concatenate(
            cls,
            results: Sequence[BoostingResult],
            dim: str = 'source',
    ):
        "Combine multiple complementary source-space BoostingResult objects"
        # result = results[0]
        out = {}
        for field in fields(cls):
            if field.name == 'version':
                continue
            elif field.name == '_isnan':
                out['_isnan'] = None
                continue
            values = [getattr(result, field.name) for result in results]
            if field.name == 't_run':
                out['t_run'] = None if any(v is None for v in values) else sum(values)
                continue
            if field.name == 'partition_results' and any(v is not None for v in values):
                if not all(v is not None for v in values):
                    raise ValueError(f'partition_results avaiable for some but not all part-results')
                new_value = [cls._eelbrain_concatenate(p_results) for p_results in zip(*values)]
            elif field.name == 'algorithm_version':
                values = set(values)
                if len(values) == 1:
                    new_value = values.pop()
                else:
                    new_value = tuple(sorted(values))
            elif any(v is None for v in values):
                new_value = None
            else:
                new_value = _concatenate_values(values, dim, field.name)
            out[field.name] = new_value
        return cls(**out)


class SplitResult:
    __slots__ = ('split', 'h', 'h_failed')

    def __init__(self, split: Split, h: np.ndarray, h_failed: np.ndarray):
        self.split = split
        self.h = h  # (n_y, n_x, n_times_h)
        self.h_failed = h_failed  # (n_y,)

    def h_with_nan(self):
        "Set failed TRFs to NaN"
        if np.any(self.h_failed):
            h = self.h.copy()
            h[self.h_failed] = np.nan
            return h
        return self.h


class Boosting:
    """Object-oriented API for boosting

    Examples
    --------
    Standard usage of the object-oriented API for a model with
    cross-validation, comparable to the :func:`boosting` function::

        data = DeconvolutionData(y, x, ds)
        data.apply_basis(0.05, 'hamming'')
        data.normalize('l1')
        data.initialize_cross_validation(5, test=1)
        model = Boosting(data)
        model.fit(0, 0.500, selective_stopping=1, error='l1')
        result = model.evaluate_fit()
    """
    # fit parameters
    tstart = None
    tstart_h = None
    tstop = None
    selective_stopping = None
    error = None
    delta = None
    mindelta = None
    # timing
    t_fit_start = None
    t_fit_done = None
    # fit result
    _i_start = None
    split_results = None
    n_skip = 0
    # eval result
    y_pred = None

    def __init__(
            self,
            data: DeconvolutionData,
    ):
        self.data = data

    def fit(
            self,
            tstart: Union[float, Sequence[float]],
            tstop: Union[float, Sequence[float]],
            selective_stopping: int = 1,
            error: str = 'l1',
            delta: float = 0.005,  # coordinate search step
            mindelta: float = None,  # narrow search by reducing delta until reaching mindelta
    ):
        self.data._check_data()
        assert error in ('l1', 'l2')
        error_id = int(error[1])
        mindelta_ = delta if mindelta is None else mindelta
        self.selective_stopping = selective_stopping
        self.error = error
        self.delta = delta
        self.mindelta = mindelta
        n_x = len(self.data.x)
        # find TRF start/stop for each x
        if isinstance(tstart, (tuple, list, np.ndarray)):
            if len(tstart) != len(self.data.x_name):
                raise ValueError(f'{tstart=}: {len(tstart)=} is different from len(x)={len(self.data.x_name)}')
            elif len(tstart) != len(tstop):
                raise ValueError(f'{tstop=}: {len(tstop)=} does not match {len(tstart)=}')
            self.tstart = tuple(tstart)
            self.tstart_h = min(self.tstart)
            self.tstop = tuple(tstop)
            if any(start >= stop for start, stop in zip(self.tstart, self.tstop)):
                raise ValueError(f"Some tstart > tstop: {tstart=} and {tstop=}")
            n_xs = [reduce(mul, map(len, xdims), 1) for _, xdims, _ in self.data._x_meta]
            tstart = [t for t, n in zip(tstart, n_xs) for _ in range(n)]
            tstop = [t for t, n in zip(tstop, n_xs) for _ in range(n)]
        else:
            if tstart >= tstop:
                raise ValueError(f"{tstart=} > {tstop=}")
            self.tstart = self.tstart_h = tstart
            self.tstop = tstop
            tstart = [tstart] * n_x
            tstop = [tstop] * n_x

        # TRF extent in indices
        tstep = self.data.time.tstep
        i_start_by_x = np.asarray([int(round(t / tstep)) for t in tstart], np.int64)
        i_stop_by_x = np.asarray([int(ceil(t / tstep)) for t in tstop], np.int64)
        self._i_start = i_start = np.min(i_start_by_x)
        i_stop = np.max(i_stop_by_x)
        h_n_times = i_stop - i_start

        if len(self.data.segments) == 1:
            self.n_skip = h_n_times - 1

        self.t_fit_start = time.time()

        # boosting
        num_threads = CONFIG['n_workers']
        split_train = package_splits([split.train for split in self.data.splits.splits])
        split_validate = package_splits([split.validate for split in self.data.splits.splits])
        split_train_and_validate = package_splits([split.train_and_validate for split in self.data.splits.splits])
        hs, hs_failed = opt.boosting_runs(self.data.y, self.data.x, self.data.x_pads, split_train, split_validate, split_train_and_validate, i_start_by_x, i_stop_by_x, delta, mindelta_, error_id, selective_stopping, num_threads)
        self.split_results = [SplitResult(split, h, h_failed) for split, h, h_failed in zip(self.data.splits.splits, hs, hs_failed)]

        self.t_fit_done = time.time()

    def _get_i_tests(self):
        assert self.data.splits.n_test
        return sorted({split.split.i_test for split in self.split_results})

    def _get_h(
            self,
            skip_failed: bool = False,
            i_test: int = None,  # test partition
    ):  # kernel and sgements on which to evaluate
        if i_test is None:
            split_results = self.split_results
            segments = self.data.segments
        else:
            split_results = [split for split in self.split_results if split.split.i_test == i_test]
            segments = split_results[0].split.test

        if skip_failed:
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', 'Mean of empty slice', RuntimeWarning)
                h = np.nanmean([split.h_with_nan() for split in split_results], 0)
            is_nan = np.isnan(h[:, :, 0])
            h[is_nan] = 0
        else:
            h = np.mean([split.h for split in split_results])
        return h, segments

    def _get_h_failed(self, i_test: int = None) -> np.ndarray:
        if i_test is None:
            split_results = self.split_results
        else:
            split_results = [split for split in self.split_results if split.split.i_test == i_test]
        out = np.all([split.h_failed for split in split_results], 0)
        if self.data.vector_dim:
            out = np.all(out.reshape((len(self.data.vector_dim), -1)), 0)
        return out

    def evaluate_fit(
            self,
            i_test: int = None,
            metrics: Sequence[str] = None,
            cross_fit: bool = None,
            partition_results: bool = False,
            debug: bool = False,
    ):
        """Compute average TRF and fit metrics

        Parameters
        ----------
        i_test
            Test partition index (only applies for models fit with
            cross-validation). By default, ``y`` in each test segment is
            predicted from the averaged model from the corresponding training
            sets, and all test segments are pooled for computing fit metrics.
            Set ``i_test`` with an integer to compute fit metrics for a single
            test partition.
        metrics
            Which model fit metrics to compute (default depends on the data).
            If no metrics are needed, ``metrics=()`` is faster.
        cross_fit
            Compute fit metrics from cross-validation (only applies to model
            with cross-validation; default ``True``).
        partition_results
            Keep results (TRFs and model evaluation) for each test-partition.
        debug
            Add additional attributes to the returned result.
        """
        if cross_fit is None:
            cross_fit = bool(self.data.splits.n_test)
        elif cross_fit and not self.data.splits.n_test:
            raise ValueError(f"{cross_fit=} for model without cross-validation")
        if partition_results and not cross_fit:
            raise ValueError(f"{partition_results=} with {cross_fit=}")

        # fit evaluation
        if metrics is None:
            if self.data.vector_dim:
                metrics = [f'vec-{self.error}', f'vec-corr']
                if self.error == 'l1':
                    metrics.append('vec-corr-l1')
            else:
                metrics = [self.error, 'r', 'r_rank']

        # test sets to use
        if cross_fit:
            if i_test is None:
                i_tests = self._get_i_tests()
            else:
                i_tests = [i_test]
        elif i_test is not None:
            raise ValueError(f"{i_test=} without cross_fit")
        else:
            i_tests = None

        # hs: [(h, test_segments), ...]
        if cross_fit:
            hs = [self._get_h(True, i) for i in i_tests]
            all_segments = np.sort(np.vstack([segments for _, segments in hs]), 0)
            eval_segments = [merge_segments(all_segments, True)]
            if partition_results:
                for _, test_segments in hs:
                    eval_segments.append(merge_segments(test_segments, True))
        else:
            hs = [self._get_h(True)]
            eval_segments = merge_segments(self.data.segments, True)
            if self.n_skip:
                eval_segments[:, 0] += self.n_skip
                # check for invalid segments (negative duration)
                valid = eval_segments[:, 1] - eval_segments[:, 0] > 0
                if not np.all(valid):
                    eval_segments = eval_segments[valid]
            eval_segments = [eval_segments]

        if metrics:
            # y dimensions
            n_y = len(self.data.y)
            if self.data.vector_dim:
                n_vec = len(self.data.vector_dim)
                n_vecs = n_y // n_vec
            else:
                n_vec = n_vecs = 0

            # predicted y
            if debug:
                y_pred_iter = y_pred = np.empty(self.data.y.shape)
            elif n_vecs:
                y_pred = np.empty((n_vec, *self.data.y.shape[1:]))
                y_pred_iter = chain.from_iterable(repeat(tuple(y_pred), n_vecs))
            else:
                y_pred = np.empty(self.data.y.shape[1:])
                y_pred_iter = repeat(y_pred, n_y)

            all_evaluators, evaluators_s, evaluators_v = get_evaluators(metrics, self.data, eval_segments)

            # fit and evaluate each y
            for i_y, y_pred_i in enumerate(y_pred_iter):
                # for cross-validation, different segments are predicted by different h:
                for h, segments in hs:
                    convolve_jit_(h[i_y], self.data.x, self.data.x_pads, self._i_start, segments, y_pred_i)

                if evaluators_s:
                    for e in evaluators_s:
                        e.add_y(i_y, self.data.y[i_y], y_pred_i)

                if evaluators_v and i_y % n_vec == n_vec - 1:
                    i_vec = i_y // n_vec
                    i_y_vec = slice(i_y-n_vec+1, i_y+1)
                    y_pred_i_vec = y_pred[i_y_vec] if debug else y_pred
                    for e in evaluators_v:
                        e.add_y(i_vec, self.data.y[i_y_vec], y_pred_i_vec)

            # Package evaluators
            evaluations = {e.attr: e.get() for e in all_evaluators}
            if debug:
                evaluations['y_pred'] = self.data.package_y_like(y_pred, 'y-pred')
            if partition_results:
                partition_evaluations = {i: {e.attr: e.get(i) for e in all_evaluators} for i in i_tests}
            else:
                partition_evaluations = None
        else:
            evaluations = {}
            partition_evaluations = {i: {} for i in i_tests}

        # package h
        h_xs = [h for h, _ in hs]
        h_x = h_xs[0] if len(h_xs) == 1 else np.mean(h_xs, 0)
        h = self.data.package_kernel(h_x, self.tstart_h)
        # package model parameters
        y_mean, y_scale, x_mean, x_scale = self.data.data_scale_ndvars()
        if debug:
            evaluations['fit'] = self
        t_run = self.t_fit_done - self.t_fit_start
        # partition-specific results
        if partition_results:
            partition_results_list = []
            for i in i_tests:
                h_i = self.data.package_kernel(h_xs[i], self.tstart_h)
                evaluations_i = partition_evaluations[i]
                result = BoostingResult(
                    self.data.y_name, self.data.x_name, self.tstart, self.tstop, bool(self.data.scale_data), self.delta, self.mindelta, self.error, self.selective_stopping,
                    y_mean, y_scale, x_mean, x_scale,
                    h_i, self._get_h_failed(i), 0,
                    self.data.basis, self.data.basis_window, None,
                    self.data.y.shape[1], self.data.y_info, self.data.ydims,
                    algorithm_version=2, i_test=i, **evaluations_i)
                partition_results_list.append(result)
        else:
            partition_results_list = None
        return BoostingResult(
            # basic parameters
            self.data.y_name, self.data.x_name, self.tstart, self.tstop, bool(self.data.scale_data), self.delta, self.mindelta, self.error, self.selective_stopping,
            # data properties
            y_mean, y_scale, x_mean, x_scale,
            # results
            h, self._get_h_failed(), t_run,
            # advanced parameters
            self.data.basis, self.data.basis_window, self.data.splits,
            # advanced data properties
            self.data.y.shape[1], self.data.y_info, self.data.ydims,
            partition_results=partition_results_list,
            algorithm_version=2,
            i_test=i_test, **evaluations)


@user_activity
def boosting(
        y: NDVarArg,
        x: Union[NDVarArg, Sequence[NDVarArg]],
        tstart: Union[float, Sequence[float]],
        tstop: Union[float, Sequence[float]],
        scale_data: Union[bool, str] = True,  # normalize y and x; can be 'inplace'
        delta: float = 0.005,  # coordinate search step
        mindelta: float = None,  # narrow search by reducing delta until reaching mindelta
        error: Literal['l1', 'l2'] = 'l2',
        basis: float = 0,
        basis_window: str = 'hamming',
        partitions: int = None,  # Number of partitionings for cross-validation
        model: CategorialArg = None,
        validate: int = 1,  # Number of segments in validation set
        test: int = 0,  # Number of segments in test set
        ds: Dataset = None,
        selective_stopping: int = 0,
        partition_results: bool = False,
        debug: bool = False,
) -> BoostingResult:
    """Estimate a linear filter with coordinate descent

    Parameters
    ----------
    y : NDVar
        Signal to predict. When ``y`` contains more than one signal (e.g.,
        multiple EEG channels), results for each signal will be computed
        independently. Muiltiple cases along a :class:`Case` dimension are
        treated as different trials which share a filter. For correlation fit
        metrics, a :class:`Space` dimension is interpreted as defining a vector
        measure.
    x : NDVar | sequence of NDVar
        Signal to use to predict ``y``. Can be sequence of NDVars to include
        multiple predictors. Time dimension must correspond to ``y``.
    tstart : scalar | sequence of scalar
        Start of the TRF in seconds. A list can be used to specify different
        values for each item in ``x``.
    tstop : scalar | sequence of scalar
        Stop of the TRF in seconds. Format must match ``tstart``.
    scale_data : bool | 'inplace'
        Scale ``y`` and ``x`` before boosting: subtract the mean and divide by
        the standard deviation (when ``error='l2'``) or the mean absolute
        value (when ``error='l1'``). Use ``'inplace'`` to save memory by scaling
        the original objects specified as ``y`` and ``x`` instead of making a 
        copy. The data scale is stored in the :class:`BoostingResult:
        :attr:`.y_mean``, :attr:`.y_scale`, :attr:`.x_mean`, and :attr:`.x_scale`
        attributes.
    delta
        Step for changes in the kernel.
    mindelta
        If the error for the training data can't be reduced, divide ``delta``
        in half until ``delta < mindelta``. The default is ``mindelta = delta``,
        i.e. ``delta`` is constant.
    error
        Error function to use (default is ``l2``).

        - ``error='l1'``: the sum of the absolute differences between ``y`` and
          ``h * x``.
        - ``error='l2'``: the sum of the squared differences between ``y`` and
          ``h * x``.

        For vector ``y``, the error is defined based on the distance in space
        for each data point.
    basis
        Use a basis of windows with this length for the kernel (by default,
        impulses are used).
    basis_window : str | scalar | tuple
        Basis window (see :func:`scipy.signal.get_window` for options; default
        is ``'hamming'``).
    partitions
        Divide the data into this many ``partitions`` for cross-validation-based
        early stopping. In each partition, ``n - 1`` segments are used for
        training, and the remaining segment is used for validation.
        If data is continuous, data are divided into contiguous segments of
        equal length (default 10).
        If data has cases, cases are divided with ``[::partitions]`` slices
        (default ``min(n_cases, 10)``; if ``model`` is specified, ``n_cases``
        is the lowest number of cases in any cell of the model).
        See :ref:`exa-data_split` example.
    model
        If data has cases, divide cases into different categories (division
        for crossvalidation is done separately for each cell).
    ds
        If provided, other parameters can be specified as string for items in
        ``ds``.
    validate
        Number of segments in validation dataset (currently has to be 1).
    test
        By default (``test=0``), the boosting algorithm uses all available data
        to estimate the kernel. Set ``test=1`` to perform *k*-fold cross-
        validation instead (with *k* = ``partitions``):
        Each partition is used as test dataset in turn, while the remaining
        ``k-1`` partitions are used to estimate the kernel. The resulting model
        fit metrics reflect the re-combination of all partitions, each one
        predicted from the corresponding, independent training set.
    selective_stopping
        By default, the boosting algorithm stops when the testing error stops
        decreasing. With ``selective_stopping=True``, boosting continues but
        excludes the predictor (one time-series in ``x``) that caused the
        increase in testing error, and continues until all predictors are
        stopped. The integer value of ``selective_stopping`` determines after
        how many steps with error increases each predictor is excluded.
    partition_results
        Keep results (TRFs and model evaluation) for each test-partition.
        This is disabled by default to reduce file size when saving results.
    debug
        Add additional attributes to the returned result.

    See Also
    --------
    plot.preview_partitions : preview data partitions for cross-validation

    Notes
    -----
    The boosting algorithm is described in [1]_.

    In order to predict data, use the :func:`convolve` function::

    >>> ds = datasets.get_uts()
    >>> ds['a1'] = epoch_impulse_predictor('uts', 'A=="a1"', ds=ds)
    >>> ds['a0'] = epoch_impulse_predictor('uts', 'A=="a0"', ds=ds)
    >>> res = boosting('uts', ['a0', 'a1'], 0, 0.5, partitions=10, model='A', ds=ds)
    >>> y_pred = convolve(res.h_scaled, ['a0', 'a1'], ds=ds)
    >>> y = ds['uts']
    >>> plot.UTS([y-y.mean('time'), y_pred], '.case')

    References
    ----------
    .. [1] David, S. V., Mesgarani, N., & Shamma, S. A. (2007). Estimating
        sparse spectro-temporal receptive fields with natural stimuli. Network:
        Computation in Neural Systems, 18(3), 191-212.
        `10.1080/09548980701609235 <https://doi.org/10.1080/09548980701609235>`_.
    """
    # scale_data
    if isinstance(scale_data, bool):
        scale_in_place = False
    elif isinstance(scale_data, str):
        if scale_data == 'inplace':
            scale_in_place = True
        else:
            raise ValueError(f"{scale_data=}")
    else:
        raise TypeError(f"{scale_data=}, need bool or str")
    # selective_stopping
    selective_stopping = int(selective_stopping)
    if selective_stopping < 0:
        raise ValueError(f"{selective_stopping=}")

    data = DeconvolutionData(y, x, ds, scale_in_place)
    data.apply_basis(basis, basis_window)
    if scale_data:
        data.normalize(error)
    data.initialize_cross_validation(partitions, model, ds, validate, test)

    fit = Boosting(data)
    fit.fit(tstart, tstop, selective_stopping, error, delta, mindelta)
    return fit.evaluate_fit(debug=debug, partition_results=partition_results)


def convolve(
        h: np.ndarray,  # (n_stims, h_n_samples)
        x: np.ndarray,  # (n_stims, n_samples)
        x_pads: np.ndarray,  # (n_stims,)
        h_i_start: int,
        segments: np.ndarray = None,  # (n_segments, 2)
        out: np.ndarray = None,
) -> np.ndarray:
    """h * x with time axis matching x

    Parameters
    ----------
    h
        H.
    x
        X.
    x_pads
        Padding for x.
    h_i_start
        Time shift of the first sample of ``h``.
    segments
        Data segments.
    out
        Buffer for predicted ``y``.
    """
    n_x, n_times = x.shape
    if segments is None:
        segments = np.array(((0, n_times),), np.int64)
    if out is None:
        out = np.empty(n_times)
    return convolve_jit_(h, x, x_pads, h_i_start, segments, out)


@numba.njit(nogil=True, cache=True)
def convolve_jit_(
        h: np.ndarray,  # (n_stims, h_n_samples)
        x: np.ndarray,  # (n_stims, n_samples)
        x_pads: np.ndarray,  # (n_stims,)
        h_i_start: int,
        segments: np.ndarray,  # (n_segments, 2)
        out: np.ndarray,
):
    h_n_times = h.shape[1]
    segments_start = segments[:, 0]
    segments_stop = segments[:, 1]
    for start, stop in zip(segments_start, segments_stop):
        out[start: stop] = 0
    h_i_stop = h_i_start + h_n_times

    # padding
    h_pad = np.sum(h * x_pads.reshape((-1, 1)), 0)
    # padding for pre-
    pad_head_n_times = max(0, h_n_times + h_i_start)
    if pad_head_n_times:
        pad_head = np.zeros(pad_head_n_times)
        for i in range(min(pad_head_n_times, h_n_times)):
            pad_head[:pad_head_n_times - i] += h_pad[- i - 1]
    # padding for post-
    pad_tail_n_times = -min(0, h_i_start)
    if pad_tail_n_times:
        pad_tail = np.zeros(pad_tail_n_times)
        for i in range(pad_tail_n_times):
            pad_tail[i:] += h_pad[i]

    for start, stop in zip(segments_start, segments_stop):
        if pad_head_n_times:
            out[start: start + pad_head_n_times] += pad_head
        if pad_tail_n_times:
            out[stop - pad_tail_n_times: stop] += pad_tail
        convolve_jit(h, x[:, start:stop], out[start:stop], h_i_start, h_i_stop)
    return out


def package_splits(splits: Sequence[np.ndarray]) -> np.ndarray:
    n = max(len(split) for split in splits)
    out = np.empty((len(splits), n, 2), np.int64)
    out.fill(-1)
    for i, split in enumerate(splits):
        out[i, :len(split)] = split
    return out
