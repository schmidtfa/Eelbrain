'''
Created on Dec 2, 2012

@author: christian
'''
import os
import cPickle as pickle
import shutil
import tempfile

import mne
from nose.tools import (assert_equal, assert_is_instance, assert_true, eq_,
                        ok_, assert_raises)
import numpy as np
from numpy.testing import assert_array_equal, assert_array_almost_equal

from eelbrain.data import datasets, Var, Factor, Dataset, Celltable, load
from eelbrain.data.data_obj import (align, align1, asvar, combine, isdatalist,
                                    isndvar, isvar, isuv, SourceSpace, UTS)


def assert_dataset_equal(ds1, ds2, msg="Datasets unequal", decimal=None):
    """
    Raise an assertion if two Datasets are not equal up to desired precision.

    Parameters
    ----------
    ds1, ds2 : Dataset
        Datasets to compare.
    msg : str
        Prefix of the error message to be printed in case of failure.
    decimal : None | int
        Desired precision (default is exact match).
    """
    assert_equal(ds1.keys(), ds2.keys(), "%s: different keys" % msg)
    for k in ds1.keys():
        assert_dataobj_equal(ds1[k], ds2[k], msg=msg, decimal=decimal)
    assert_equal(ds1.info.keys(), ds2.info.keys(), "%s: keys in info" % msg)


def assert_dataobj_equal(d1, d2, msg="Data-objects unequal", decimal=None):
    """
    Raise an assertion if two data-objects are not equal up to desired
    precision.

    Parameters
    ----------
    ds1, ds2 : data-objects
        Data-objects to compare.
    msg : str
        Prefix of the error message to be printed in case of failure.
    decimal : None | int
        Desired precision (default is exact match).
    """
    msg = "%s:" % msg
    assert_equal(d1.name, d2.name, "%s unequal names (%r vs %r"
                 ")" % (msg, d1.name, d2.name))
    msg += ' %r have' % d1.name
    assert_equal(len(d1), len(d2), "%s unequal length" % msg)
    if isvar(d1) and decimal:
        assert_array_almost_equal(d1.x, d2.x, decimal)
    elif isuv(d1):
        assert_true(np.all(d1 == d2), "%s unequal values: %r vs "
                    "%r" % (msg, d1, d2))
    elif isndvar(d1):
        assert_true(np.all(d1.x == d2.x), "%s unequal values" % msg)
    elif isdatalist(d1):
        for i in xrange(len(d1)):
            assert_equal(d1[i], d2[i], "%s unequal values" % msg)


def assert_source_space_equal(src1, src2, msg="SourceSpace Dimension objects "
                              "unequal"):
    """
    Raise an assertion if two SourceSpace objects are not equal up to desired
    precision.

    Parameters
    ----------
    src1, src2 : SourceSpace objects
        SourceSpace objects to compare.
    msg : str
        Prefix of the error message to be printed in case of failure.
    """
    msg = "%s:" % msg
    assert_array_equal(src1.vertno[0], src2.vertno[0], "%s unequal lh vertno "
                       "(%r vs %r)" % (msg, src1.vertno[0], src2.vertno[0]))
    assert_array_equal(src1.vertno[1], src2.vertno[1], "%s unequal rh vertno "
                       "(%r vs %r)" % (msg, src1.vertno[1], src2.vertno[1]))
    assert_equal(src1.subject, src2.subject, "%s unequal subject (%r vs %r"
                 ")" % (msg, src1.subject, src2.subject))
    assert_equal(src1.src, src2.src, "%s unequal names (%r vs %r"
                 ")" % (msg, src1.src, src2.src))
    assert_equal(src1.subjects_dir, src2.subjects_dir, "%s unequal names (%r "
                 "vs %r)" % (msg, src1.subjects_dir, src2.subjects_dir))


def test_print():
    "Run the string representation methods"
    ds = datasets.get_rand()
    print ds
    print repr(ds)
    A = ds['A']
    print A
    print repr(A)
    Y = ds['Y']
    print Y
    print repr(Y)
    Ynd = ds['uts']
    print Ynd
    print repr(Ynd)


def test_align():
    "Testing align() and align1() functions"
    ds = datasets.get_rand()
    ds.index()
    idx = np.arange(0, ds.n_cases, 4)
    ds_sub = ds.sub(np.arange(0, ds.n_cases, 2))
    dsa = align1(ds_sub, idx)
    assert_array_equal(dsa['index'].x, idx, "align1() failure")

    dsa1, dsa2 = align(dsa, ds_sub)
    assert_array_equal(dsa1['index'].x, dsa2['index'].x, "align() failed")


def test_celltable():
    "Test the Celltable class."
    ds = datasets.get_rand()
    ds['cat'] = Factor('abcd', rep=15)

    ct = Celltable('Y', 'A', ds=ds)
    eq_(ct.n_cases, 60)
    eq_(ct.n_cells, 2)

    ct = Celltable('Y', 'A', match='rm', ds=ds)
    eq_(ct.n_cases, 30)
    eq_(ct.n_cells, 2)

    ct = Celltable('Y', 'cat', cat=('c', 'b'), ds=ds)
    eq_(ct.n_cases, 30)
    eq_(ct.X[0], 'c')
    eq_(ct.X[-1], 'b')

    ct = Celltable('Y', 'A', match='rm', ds=ds)
    eq_(ct.n_cases, 30)
    assert np.all(ct.groups['a0'] == ct.groups['a1'])

    ct = Celltable('Y', 'cat', match='rm', cat=('c', 'b'), ds=ds)
    eq_(ct.n_cases, 30)
    eq_(ct.X[0], 'c')
    eq_(ct.X[-1], 'b')

    # test coercion
    ct = Celltable(ds['Y'].x, 'A', ds=ds)
    assert_is_instance(ct.Y, np.ndarray)
    ct = Celltable(ds['Y'].x, 'A', ds=ds, coercion=asvar)
    assert_is_instance(ct.Y, Var)

    # test sub
    ds_sub = ds.sub("A == 'a0'")
    ct_sub = Celltable('Y', 'B', ds=ds_sub)
    ct = Celltable('Y', 'B', sub="A == 'a0'", ds=ds)
    assert_dataobj_equal(ct_sub.Y, ct.Y)

    # test sub with rm
    ct_sub = Celltable('Y', 'B', match='rm', ds=ds_sub)
    ct = Celltable('Y', 'B', match='rm', sub="A == 'a0'", ds=ds)
    assert_dataobj_equal(ct_sub.Y, ct.Y)

    # test rm sorting
    ds = Dataset()
    ds['rm'] = Factor('abc', rep=4)
    ds['Y'] = Var(np.arange(3.).repeat(4))
    ds['X'] = Factor('ab', rep=2, tile=3)
    idx = np.arange(12)
    np.random.shuffle(idx)
    ds = ds[idx]
    ct = Celltable('Y', 'X', 'rm', ds=ds)
    assert_array_equal(ct.match, Factor('abc', tile=2))
    assert_array_equal(ct.Y, np.tile(np.arange(3.), 2))
    assert_array_equal(ct.X, Factor('ab', rep=3))


def test_combine():
    "Test combine()"
    ds1 = datasets.get_rand()
    ds2 = datasets.get_rand()
    ds = combine((ds1, ds2))
    assert_array_equal(ds2['Y'].x, ds['Y'].x[ds1.n_cases:], "Basic combine")
    del ds1['Y']
    del ds2['YCat']
    ds = combine((ds1, ds2))
    assert_array_equal(ds2['Y'].x, ds['Y'].x[ds1.n_cases:], "Combine with "
                       "missing Var")
    assert_true(np.all(ds1['YCat'] == ds['YCat'][:ds1.n_cases]), "Combine "
                "with missing Factor")

    # combine NDVar with unequel dimensions
    ds = datasets.get_rand(utsnd=True)
    y = ds['utsnd']
    y1 = y.sub(sensor=['0', '1', '2', '3'])
    y2 = y.sub(sensor=['1', '2', '3', '4'])
    ds1 = Dataset(y1)
    ds2 = Dataset(y2)
    dsc = combine((ds1, ds2))
    y = dsc['utsnd']
    assert_equal(y.sensor.names, ['1', '2', '3'], "Sensor dimension "
                 "intersection failed.")
    dims = ('case', 'sensor', 'time')
    ref = np.concatenate((y1.get_data(dims)[:, 1:], y2.get_data(dims)[:, :3]))
    assert_array_equal(y.get_data(dims), ref, "combine utsnd")


def test_dataset_combining():
    "Test Dataset combination methods"
    ds = datasets.get_uv()
    del ds['fltvar'], ds['intvar'], ds['A']

    ds2 = datasets.get_uv()
    del ds2['fltvar'], ds2['intvar']
    ds.update(ds2)
    assert_array_equal(ds['A'], ds2['A'])

    ds2 = datasets.get_uv()
    del ds2['fltvar'], ds2['intvar']
    ds2['B'][5] = 'something_else'
    del ds['A']
    assert_raises(ValueError, ds.update, ds2)


def test_dataset_sorting():
    "Test Dataset sorting methods"
    test_array = np.arange(10)
    ds = Dataset()
    ds['v'] = Var(test_array)
    ds['f'] = Factor(test_array)

    # shuffle the Dataset
    rand_idx = test_array.copy()
    np.random.shuffle(rand_idx)
    ds_shuffled = ds[rand_idx]

    # ascending, Var, copy
    dsa = ds_shuffled.sorted('v')
    assert_dataset_equal(dsa, ds, "Copy sorted by Var, ascending")

    # descending, Factor, in-place
    ds_shuffled.sort('f', descending=True)
    assert_dataset_equal(ds_shuffled, ds[::-1], "In-place sorted by Factor, "
                         "descending")


def test_dataset_indexing():
    """Test Dataset indexing"""
    ds = datasets.get_uv()
    ds['C', :] = 'c'
    ok_(np.all(ds.eval("C == 'c'")))


def test_dim_uts():
    "Test UTS Dimension"
    uts = UTS(-0.1, 0.005, 301)

    # make sure indexing rounds correctly for floats
    for i, s in enumerate(np.arange(0, 1.4, 0.05)):
        idx = uts.dimindex((-0.1 + s, s))
        assert_equal(idx.start, 10 * i)
        assert_equal(idx.stop, 20 + 10 * i)


def test_ndvar():
    "Test the NDVar class"
    ds = datasets.get_rand(utsnd=True)
    x = ds['utsnd']

    # slicing
    assert_raises(KeyError, x.sub, sensor='5')
    assert_equal(x.sub(sensor='4').ndim, 2)
    assert_equal(x.sub(sensor=['4']).ndim, 3)
    assert_equal(x.sub(case=1, sensor='4').ndim, 1)

    # baseline correction
    x_bl = x - x.summary(time=(None, 0))
    # assert that the baseline is 0
    bl = x_bl.summary('case', 'sensor', time=(None, 0))
    ok_(abs(bl) < 1e-10, "Baseline correction")

    # NDVar as index
    sens_mean = x.mean(('case', 'time'))
    idx = sens_mean > 0
    pos = sens_mean[idx]
    assert_array_equal(pos.x > 0, True)


def test_io_pickle():
    "Test io by pickling"
    ds = datasets.get_rand()
    ds.info['info'] = "Some very useful information about the Dataset"
    tempdir = tempfile.mkdtemp()
    try:
        dest = os.path.join(tempdir, 'test.pickled')
        with open(dest, 'wb') as fid:
            pickle.dump(ds, fid, protocol=pickle.HIGHEST_PROTOCOL)
        with open(dest, 'rb') as fid:
            ds2 = pickle.load(fid)
    finally:
        shutil.rmtree(tempdir)

    assert_dataset_equal(ds, ds2)


def test_io_txt():
    "Test Dataset io as text"
    ds = datasets.get_uv()

    # Var that has integer values as float
    ds['intflt'] = ds.eval('intvar * 1.')
    ds['intflt'].name = 'intflt'

    # io test
    tempdir = tempfile.mkdtemp()
    try:
        dest = os.path.join(tempdir, 'test.txt')
        ds.save_txt(dest)
        ds2 = load.tsv(dest)
    finally:
        shutil.rmtree(tempdir)

    assert_dataset_equal(ds, ds2, decimal=6)


def test_source_space():
    "Test SourceSpace Dimension"
    subject = 'fsaverage'
    data_path = mne.datasets.sample.data_path()
    mri_sdir = os.path.join(data_path, 'subjects')
    mri_dir = os.path.join(mri_sdir, subject)
    src_path = os.path.join(mri_dir, 'bem', subject + '-ico-5-src.fif')
    label_dir = os.path.join(mri_dir, 'label')
    label_ba1 = mne.read_label(os.path.join(label_dir, 'lh.BA1.label'))
    label_v1 = mne.read_label(os.path.join(label_dir, 'lh.V1.label'))
    label_mt = mne.read_label(os.path.join(label_dir, 'lh.MT.label'))
    label_ba1_v1 = label_ba1 + label_v1
    label_v1_mt = label_v1 + label_mt

    src = mne.read_source_spaces(src_path)
    source = SourceSpace((src[0]['vertno'], src[1]['vertno']), subject,
                         'ico-5', mri_sdir)
    index = source.dimindex(label_v1)
    source_v1 = source[index]
    index = source.dimindex(label_ba1_v1)
    source_ba1_v1 = source[index]
    index = source.dimindex(label_v1_mt)
    source_v1_mt = source[index]
    index = source_ba1_v1.dimindex(source_v1_mt)
    source_v1_intersection = source_ba1_v1[index]
    assert_source_space_equal(source_v1, source_v1_intersection)

    # index from label
    index = source.index_for_label(label_v1)
    assert_array_equal(index.source[index.x].vertno[0],
                       np.intersect1d(source.lh_vertno, label_v1.vertices, 1))

    # parcellation and cluster localization
    if mne.__version__ < '0.8':
        return
    parc = mne.read_annot(subject, parc='aparc', subjects_dir=mri_sdir)
    indexes = [source.index_for_label(label) for label in parc
               if len(label) > 10]
    x = np.vstack([index.x for index in indexes])
    ds = source._cluster_properties(x)
    for i in xrange(ds.n_cases):
        assert_equal(ds[i, 'location'], parc[i].name)


def test_var():
    "Test Var objects"
    base = Factor('aabbcde')
    Y = Var.from_dict(base, {'a': 5, 'e': 8}, default=0)
    assert_array_equal(Y.x, [5, 5, 0, 0, 0, 0, 8])
