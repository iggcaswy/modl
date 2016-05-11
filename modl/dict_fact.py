"""
Author: Arthur Mensch (2016)
Dictionary learning with masked data
"""
from math import ceil

import numpy as np
from scipy import linalg
from sklearn.base import BaseEstimator
from sklearn.utils import check_random_state, gen_batches, check_array
from sklearn.utils._random import sample_without_replacement

from modl._utils.enet_proj import enet_projection, enet_scale, enet_norm
from .dict_fact_fast import _update_dict, _update_code, _get_weights, \
    _get_simple_weights


class DictMF(BaseEstimator):
    """Matrix factorization estimator based on masked online dictionary
     learning.

    Parameters
    ----------
    alpha: float,
        Regularization of the code (ridge penalty)
    n_components: int,
        Number of components for the dictionary
    learning_rate: float in [0.5, 1],
        Controls the sequence of weights in
         the update of the surrogate function
    batch_size: int,
        Number of samples to consider between each dictionary update
    offset: float,
        Offset in the
    reduction: float,
        Sets how much the data is masked during the algorithm
    fit_intercept: boolean,
        Fixes the first dictionary atom to [1, .., 1]
    dict_init: ndarray (n_components, n_cols),
        Initial dictionary
    l1_ratio: float in [0, 1]:
        Controls the sparsity of the dictionary
    var_red: boolean,
        Updates the Gram matrix online (Experimental, non tested)
    max_n_iter: int,
        Number of samples to visit before stopping. If None, fit performs
         a single epoch on data
    random_state: int or RandomState
        Pseudo number generator state used for random sampling.
    verbose: boolean,
        Degree of output the procedure will print.
    backend: str in {'c', 'python'},
        'c' is fastter, but 'python' is easier to hack
    debug: boolean,
        Keep tracks of the surrogate loss during the procedure
    callback: callable,
        Function to be called when printing information

    Attributes
    -------
        self.Q_: ndarray (n_components, n_cols):
            Learned dictionary
    """

    def __init__(self, alpha=1.0,
                 n_components=30,
                 # Hyper-parameters
                 learning_rate=1.,
                 batch_size=1,
                 offset=0,
                 # Reduction parameter
                 reduction=1,
                 var_red='none',
                 projection='full',
                 fit_intercept=False,
                 # Dict parameter
                 dict_init=None,
                 l1_ratio=0,
                 # For variance reduction
                 n_samples=None,
                 # Generic parameters
                 max_n_iter=0,
                 random_state=None,
                 verbose=0,
                 backend='c',
                 debug=False,
                 callback=None):

        self.fit_intercept = fit_intercept

        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.offset = offset

        self.reduction = reduction
        self.alpha = alpha
        self.l1_ratio = l1_ratio

        self.dict_init = dict_init
        self.n_components = n_components

        self.var_red = var_red

        self.n_samples = n_samples
        self.max_n_iter = max_n_iter

        self.random_state = random_state
        self.verbose = verbose

        self.backend = backend
        self.debug = debug

        self.callback = callback

        self.projection = projection

    @property
    def components_(self):
        return self.D_

    def _get_var_red(self):
        var_reds = {
            'none': 1,
            'code_only': 2,
            'weight_based': 3,
            'sample_based': 4,
        }
        return var_reds[self.var_red]

    def _get_projection(self):
        projections = {
            'full': 1,
            'partial': 2,
        }
        return projections[self.projection]

    def _init(self, X):
        """Initialize statistic and dictionary"""
        if self.var_red not in ['none', 'code_only', 'weight_based',
                                'sample_based']:
            raise ValueError("var_red should be in {'none', 'code_only',"
                             " 'weight_based', 'sample_based'},"
                             " got %s" % self.var_red)
        if self.projection not in ['partial', 'full']:
            raise ValueError("projection should be in {'partial', 'full'},"
                             " got %s" % self.projection)

        X = check_array(X, dtype='float', order='F')

        n_rows, n_cols = X.shape

        if self.n_samples is not None:
            n_samples = self.n_samples
        else:
            n_samples = n_rows

        self.random_state_ = check_random_state(self.random_state)

        # D dictionary
        if self.dict_init is not None:
            if self.dict_init.shape != (self.n_components, n_cols):
                raise ValueError(
                    'Initial dictionary and X shape mismatch: %r != %r' % (
                        self.dict_init.shape,
                        (self.n_components, n_cols)))
            self.D_ = check_array(self.dict_init, order='C',
                                  dtype='float', copy=True)
            if self.fit_intercept:
                if not (np.all(self.D_[0] == self.D_[0].mean())):
                    raise ValueError('When fitting intercept and providing '
                                     'initial dictionary, first component of'
                                     ' the dictionary should be '
                                     'proportional to [1, ..., 1]')
                self.D_[0] = 1
        else:
            self.D_ = np.empty((self.n_components, n_cols), order='C')

            if self.fit_intercept:
                self.D_[0] = 1
                self.D_[1:] = self.random_state_.randn(
                    self.n_components - 1,
                    n_cols)
            else:
                self.D_[:] = self.random_state_.randn(
                    self.n_components,
                    n_cols)

        self.D_ = np.asfortranarray(
            enet_scale(self.D_, l1_ratio=self.l1_ratio, radius=1))

        self.A_ = np.zeros((self.n_components, self.n_components),
                           order='F')
        self.B_ = np.zeros((self.n_components, n_cols), order="F")

        self.counter_ = np.zeros(n_cols + 1, dtype='int')

        self.n_iter_ = 0

        if self.var_red != 'weight_based':
            self.G_ = self.D_.dot(self.D_.T).T
            self.multiplier_ = np.array([1.])

        if self.var_red in ['code_only', 'sample_based']:
            self.row_counter_ = np.zeros(n_samples, dtype='int')
            self.beta_ = np.zeros((n_samples, self.n_components),
                                  order="F")

        self.code_ = np.zeros((n_samples, self.n_components))

        if self.backend == 'c':
            if not hasattr(self, 'row_counter_'):
                self.row_counter_ = np.zeros(1, dtype='int')
            if not hasattr(self, 'G_'):
                self.G_ = np.zeros((1, 1), order='F')
            if not hasattr(self, 'beta_'):
                self.beta_ = np.zeros((1, 1), order='F')
            if not hasattr(self, 'multiplier_'):
                self.multiplier_ = np.zeros(1)

        if self.debug:
            self.loss_ = np.empty(self.max_n_iter)
            self.loss_indep_ = 0.

    def _check_init(self):
        return hasattr(self, 'D_')

    def _reset_stat(self):
        if (self.var_red in ['code_only, sample_based'] and
                self.multiplier_[0] < 1e-50):
            self.A_ *= self.multiplier_[0]
            self.B_ *= self.multiplier_[0]
            self.multiplier_[0] = 1.

    def fit(self, X, y=None):
        """Use X to learn a dictionary Q_. The algorithm cycles on X
        until it reaches the max number of iteration

        Parameters
        ----------
        X: ndarray (n_samples, n_features)
            Dataset to learn the dictionary from
        """
        X = check_array(X, dtype='float', order='F')
        if self.max_n_iter > 0:
            self.partial_fit(X, check_input=False)
            while self.n_iter_ + self.batch_size - 1 < self.max_n_iter:
                self.partial_fit(X, check_input=False)
        else:
            # Default to one pass
            self.partial_fit(X, check_input=False)

    def _refit(self, X):
        """Use X and Q to learn a code P"""
        self.code_ = self.transform(X)

    def transform(self, X, y=None):
        """Computes the loadings to reconstruct dataset X
        from the dictionary Q

        Parameters
        ----------
        X: ndarray (n_samples, n_features)
            Dataset to learn the code from

        Returns
        -------
        code: ndarray(n_samples, n_components)
            Code obtained projecting X on the dictionary
        """
        X = check_array(X, order='F')
        if self.var_red != 'weight_based':
            G = self.G_.copy()
        else:
            G = self.D_.dot(self.D_.T)
        Dx = self.D_.dot(X.T)
        G.flat[::self.n_components + 1] += 2 * self.alpha
        P = linalg.solve(G, Dx, sym_pos=True,
                         overwrite_a=True, check_finite=False)
        return P

    def _reduced_transform(self, X):
        n_rows, n_cols = X.shape
        G = self.G_.copy()
        G.flat[::self.n_components + 1] += 2 * self.alpha
        subset_size = int(ceil(n_cols / self.reduction))
        batches = gen_batches(len(X), self.batch_size)
        row_range = self.random_state_.permutation(n_rows)
        P = np.zeros((n_rows, self.n_components), order='C')
        for batch in batches:
            sample_subset = row_range[batch]
            subset = sample_without_replacement(n_cols,
                                                subset_size,
                                                random_state=
                                                self.random_state_)
            self.row_counter_[sample_subset] += 1
            this_X = X[sample_subset][:, subset] * self.reduction

            Dx = self.D_[:, subset].dot(this_X.T)
            P[batch] = linalg.solve(G, Dx, sym_pos=True,
                                    overwrite_a=True,
                                    check_finite=False).T
        return P

    def partial_fit(self, X, y=None, sample_subset=None, check_input=True):
        """Stream data X to update the estimator dictionary

        Parameters
        ----------
        X: ndarray (n_samples, n_features)
            Dataset to learn the code from

        """
        if self.backend not in ['python', 'c']:
            raise ValueError("Invalid backend %s" % self.backend)

        if self.debug and self.backend == 'c':
            raise NotImplementedError(
                "Recording objective loss is only available"
                "with backend == 'python'")

        if not self._check_init():
            self._init(X)

        if check_input:
            X = check_array(X, dtype='float', order='F')
        n_rows, n_cols = X.shape

        if sample_subset is None:
            sample_subset = np.arange(n_rows)

        old_n_iter = self.n_iter_
        n_verbose_call = 0

        row_range = np.arange(n_rows)
        len_subset = int(ceil(n_cols / self.reduction))

        self.random_state_.shuffle(row_range)
        batches = gen_batches(len(row_range), self.batch_size)

        if self.fit_intercept:
            D_range = np.arange(1, self.n_components)
        else:
            D_range = np.arange(self.n_components)

        if self.backend == 'c':
            # Init various arrays for efficiency
            D_subset = np.empty((self.n_components, len_subset),
                                order='F')

            X_temp = np.empty((self.batch_size, len_subset),
                              order='F')
            G_temp = np.empty((self.n_components, self.n_components),
                              order='F')
            code_temp = np.empty((self.n_components, self.batch_size),
                                 order='F')
            w_temp = np.zeros(len_subset + 1)
            R = np.empty((self.n_components, n_cols), order='F')
            norm_temp = np.zeros(self.n_components)
            if self.projection == 'full':
                proj_temp = np.zeros(n_cols)
            else:
                proj_temp = np.zeros(len_subset)

        for batch in batches:
            row_batch = row_range[batch]
            if 0 < self.max_n_iter <= self.n_iter_ + len(row_batch) - 1:
                return
            subset = sample_without_replacement(n_cols, len_subset,
                                                random_state=
                                                self.random_state_)
            this_X = X[row_batch]
            if self.backend == 'c':
                this_X = np.asfortranarray(this_X)
                _update_code(this_X,
                             subset,
                             sample_subset[row_batch],
                             self.alpha,
                             self.learning_rate,
                             self.offset,
                             self._get_var_red(),
                             self._get_projection(),
                             self.reduction,
                             self.D_,
                             self.code_,
                             self.A_,
                             self.B_,
                             self.G_,
                             self.beta_,
                             self.multiplier_,
                             self.counter_,
                             self.row_counter_,
                             D_subset,
                             X_temp,
                             code_temp,
                             G_temp,
                             w_temp)
            else:
                self._update_code_slow(this_X,

                                       subset,
                                       sample_subset[row_batch])
            dict_subset = subset
            self._reset_stat()

            self.random_state_.shuffle(D_range)
            # Dictionary update
            if self.backend == 'c':
                _update_dict(self.D_,
                             dict_subset,
                             self.fit_intercept,
                             self.l1_ratio,
                             self._get_projection(),
                             self._get_var_red(),
                             D_range,
                             self.A_,
                             self.B_,
                             self.G_,
                             R,
                             D_subset,
                             norm_temp,
                             proj_temp)
            else:
                self._update_dict_slow(dict_subset, D_range)
            self.n_iter_ += len(row_batch)

            if self.verbose and (self.n_iter_ - old_n_iter) // ceil(
                    int(n_rows / self.verbose)) == n_verbose_call:
                print("Iteration %i" % self.n_iter_)
                n_verbose_call += 1
                if self.callback is not None:
                    self.callback(self)

    def _update_code_slow(self, X, subset,
                          sample_subset):
        """Compute code for a mini-batch and update algorithm statistics accordingly

        Parameters
        ----------
        this_X: ndarray, (batch_size, len_subset)
            Mini-batch of masked data to perform the update from
        this_subset: ndarray (len_subset),
            Mask used on X
        sample_subset: ndarray (batch_size),
            Sample indices of this_X within X
        """
        this_X = X[:, subset]
        batch_size, _ = this_X.shape
        _, n_cols = self.D_.shape

        D_subset = self.D_[:, subset]

        self.counter_[0] += batch_size

        if self.var_red == 'weight_based':
            self.counter_[subset + 1] += batch_size
            w = np.zeros(len(subset) + 1)
            _get_weights(w, subset, self.counter_, batch_size,
                         self.learning_rate, self.offset)
            w_A = w[0]
            w_B = w[1:]
            Dx = np.dot(D_subset, this_X.T)
            this_G = D_subset.dot(D_subset.T)
            this_G.flat[::self.n_components + 1] += self.alpha / self.reduction
            this_beta = Dx
            this_code = linalg.solve(this_G,
                                     this_beta,
                                     sym_pos=True, overwrite_a=True,
                                     check_finite=False)
            self.A_ *= 1 - w_A
            self.A_ += this_code.dot(this_code.T) * w_A / batch_size
            self.B_[:, subset] *= 1 - w_B
            self.B_[:, subset] += this_code.dot(this_X) * w_B / batch_size

        else:  # self.var_red in ['none', 'code_only', 'sample_based']
            this_X *= self.reduction
            Dx = np.dot(D_subset, this_X.T)
            w = _get_simple_weights(subset, self.counter_, batch_size,
                                    self.learning_rate, self.offset)
            if w != 1:
                self.multiplier_[0] *= 1 - w

            w_norm = w / self.multiplier_[0]

            if self.projection == 'partial':
                this_G = self.G_.copy()
            else:
                this_G = self.G_

            this_G.flat[::self.n_components + 1] += self.alpha

            if self.var_red == 'none':
                this_beta = Dx
            else:
                self.row_counter_[sample_subset] += 1
                w_beta = np.power(self.row_counter_[sample_subset]
                                  [:, np.newaxis], -self.learning_rate)
                self.beta_[sample_subset] *= 1 - w_beta
                self.beta_[sample_subset] += Dx.T * w_beta
                this_beta = self.beta_[sample_subset].T

            this_code = linalg.solve(this_G,
                                     this_beta,
                                     sym_pos=True, overwrite_a=True,
                                     check_finite=False)

            self.A_ += this_code.dot(this_code.T) * w_norm / batch_size

            if self.var_red == 'sample_based':
                self.B_ += this_code.dot(X) * w_norm / batch_size
            else:
                self.B_[:, subset] += this_code.dot(
                    this_X) * w_norm / batch_size

        self.code_[sample_subset] = this_code.T

        if self.debug:
            dict_loss = .5 * np.sum(
                self.D_.dot(self.D_.T) * self.A_) - np.sum(
                self.D_ * self.B_)
            self.loss_indep_ *= (1 - w)
            self.loss_indep_ += (.5 * np.sum(this_X ** 2) +
                                 self.alpha * np.sum(this_code ** 2)) * w
            self.loss_[self.n_iter_] = self.loss_indep_ + dict_loss

    def _update_dict_slow(self, subset, D_range):
        """Update dictionary from statistic
        Parameters
        ----------
        subset: ndarray (len_subset),
            Mask used on X
        Q: ndarray (n_components, n_features):
            Dictionary to perform ridge regression
        l1_ratio: float in [0, 1]:
            Controls the sparsity of the dictionary
        stat: DictMFStats,
            Statistics kept by the algorithm, to be updated by the function
        var_red: boolean,
            Online update of the Gram matrix (Experimental)
        random_state: int or RandomState
            Pseudo number generator state used for random sampling.

        """
        D_subset = self.D_[:, subset]

        if self.projection == 'full':
            norm = enet_norm(self.D_, self.l1_ratio)
        else:
            if self.var_red != 'weight_based':
                self.G_ -= D_subset.dot(D_subset.T)
            norm = enet_norm(D_subset, self.l1_ratio)
        R = self.B_[:, subset] - np.dot(D_subset.T, self.A_).T

        ger, = linalg.get_blas_funcs(('ger',), (self.A_, D_subset))
        for k in D_range:
            ger(1.0, self.A_[k], D_subset[k], a=R, overwrite_a=True)
            # R += np.dot(stat.A[:, j].reshape(n_components, 1),
            D_subset[k] = R[k] / (self.A_[k, k])
            if self.projection == 'full':
                self.D_[k][subset] = D_subset[k]
                self.D_[k] = enet_projection(self.D_[k],
                                             norm[k],
                                             self.l1_ratio)
                D_subset[k] = self.D_[k][subset]
            else:
                D_subset[k] = enet_projection(D_subset[k], norm[k],
                                              self.l1_ratio)
            ger(-1.0, self.A_[k], D_subset[k], a=R, overwrite_a=True)
            # R -= np.dot(stat.A[:, j].reshape(n_components, 1),
        if self.projection == 'partial':
            self.D_[:, subset] = D_subset
            if self.var_red != 'weight_based':
                self.G_ += D_subset.dot(D_subset.T)
        elif self.var_red != 'weight_based':
            self.G_ = self.D_.dot(self.D_.T).T

    def _callback(self):
        if self.callback is not None:
            self.callback(self)
