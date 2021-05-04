"""Stacking classifier and regressor."""
# This patch is taken directly from:
#   https://github.com/scikit-learn/scikit-learn/pull/16748/

# Authors: Guillaume Lemaitre <g.lemaitre58@gmail.com>
# License: BSD 3 clause

from abc import ABCMeta, abstractmethod
from copy import deepcopy

import numpy as np
from joblib import Parallel, delayed
import scipy.sparse as sparse

from sklearn.base import clone
from sklearn.base import ClassifierMixin, RegressorMixin, TransformerMixin
from sklearn.base import is_classifier, is_regressor

from sklearn.ensemble._base import _fit_single_estimator
from sklearn.ensemble._base import _BaseHeterogeneousEnsemble

from sklearn.linear_model import LogisticRegression
from sklearn.linear_model import RidgeCV

from sklearn.model_selection import cross_val_predict
from sklearn.model_selection import StratifiedKFold
from sklearn.model_selection import check_cv

from sklearn.preprocessing import LabelEncoder

from sklearn.utils import Bunch
from sklearn.utils.metaestimators import if_delegate_has_method
from sklearn.utils.multiclass import check_classification_targets
from sklearn.utils.validation import check_is_fitted
from sklearn.utils.validation import column_or_1d
from sklearn.utils.validation import check_array


class _BaseStacking(TransformerMixin, _BaseHeterogeneousEnsemble,
                    metaclass=ABCMeta):
    """Base class for stacking method."""

    @abstractmethod
    def __init__(self, estimators, final_estimator=None, cv=None,
                 stack_method='auto', n_jobs=None, verbose=0,
                 passthrough=False):
        super().__init__(estimators=estimators)
        self.final_estimator = final_estimator
        self.cv = cv
        self.stack_method = stack_method
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.passthrough = passthrough

    def _clone_final_estimator(self, default):
        if self.final_estimator is not None:
            self.final_estimator_ = clone(self.final_estimator)
        else:
            self.final_estimator_ = clone(default)

    def _concatenate_predictions(self, X, predictions):
        """Concatenate the predictions of each first layer learner and
        possibly the input dataset `X`.

        If `X` is sparse and `self.passthrough` is False, the output of
        `transform` will be dense (the predictions). If `X` is sparse
        and `self.passthrough` is True, the output of `transform` will
        be sparse.

        This helper is in charge of ensuring the predictions are 2D arrays and
        it will drop one of the probability column when using probabilities
        in the binary case. Indeed, the p(y|c=0) = 1 - p(y|c=1)
        """
        X_meta = []
        for est_idx, preds in enumerate(predictions):
            # case where the the estimator returned a 1D array
            if preds.ndim == 1:
                X_meta.append(preds.reshape(-1, 1))
            else:
                if (self.stack_method_[est_idx] == 'predict_proba' and
                        len(self.classes_) == 2):
                    # Remove the first column when using probabilities in
                    # binary classification because both features are perfectly
                    # collinear.
                    X_meta.append(preds[:, 1:])
                else:
                    X_meta.append(preds)
        if self.passthrough:
            X_meta.append(X)
            if sparse.issparse(X):
                return sparse.hstack(X_meta, format=X.format)

        return np.hstack(X_meta)

    @staticmethod
    def _method_name(name, estimator, method):
        if estimator == 'drop':
            return None
        if method == 'auto':
            if getattr(estimator, 'predict_proba', None):
                return 'predict_proba'
            elif getattr(estimator, 'decision_function', None):
                return 'decision_function'
            else:
                return 'predict'
        else:
            if not hasattr(estimator, method):
                raise ValueError('Underlying estimator {} does not implement '
                                 'the method {}.'.format(name, method))
            return method

    def fit(self, X, y, sample_weight=None):
        """Fit the estimators.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where `n_samples` is the number of samples and
            `n_features` is the number of features.

        y : array-like of shape (n_samples,)
            Target values.

        sample_weight : array-like of shape (n_samples,) or default=None
            Sample weights. If None, then samples are equally weighted.
            Note that this is supported only if all underlying estimators
            support sample weights.

            .. versionchanged:: 0.23
               when not None, `sample_weight` is passed to all underlying
               estimators

        Returns
        -------
        self : object
        """
        # all_estimators contains all estimators, the one to be fitted and the
        # 'drop' string.
        names, all_estimators = self._validate_estimators()
        self._validate_final_estimator()

        stack_method = [self.stack_method] * len(all_estimators)

        if self.cv == "prefit":
            self.estimators_ = []
            for estimator in all_estimators:
                # check_is_fitted(estimator)
                # TODO: uncomment once bug #18648 is fixed
                self.estimators_.append(estimator)
        else:
            # Fit the base estimators on the whole training data. Those
            # base estimators will be used in transform, predict, and
            # predict_proba. They are exposed publicly.
            self.estimators_ = Parallel(n_jobs=self.n_jobs)(
                delayed(_fit_single_estimator)(clone(est), X, y, sample_weight)
                for est in all_estimators if est != 'drop'
            )

        self.n_features_in_ = self.estimators_[0].n_features_in_

        self.named_estimators_ = Bunch()
        est_fitted_idx = 0
        for name_est, org_est in zip(names, all_estimators):
            if org_est != 'drop':
                self.named_estimators_[name_est] = self.estimators_[
                    est_fitted_idx]
                est_fitted_idx += 1
            else:
                self.named_estimators_[name_est] = 'drop'

        self.stack_method_ = [
            self._method_name(name, est, meth)
            for name, est, meth in zip(names, all_estimators, stack_method)
        ]

        # To train the meta-classifier using the most data as possible, we use
        # a cross-validation to obtain the output of the stacked estimators.

        if self.cv == "prefit":
            # Generate predictions from prefit models
            def _predict_single_estimator(estimator, X, method):
                func = getattr(estimator, method)
                return func(X)

            predictions = [
                _predict_single_estimator(est, X, method=meth)
                for est, meth in zip(all_estimators, self.stack_method_)
                if est != 'drop'
            ]
        else:
            # To ensure that the data provided to each estimator are the same,
            # we need to set the random state of the cv if there is one and we
            # need to take a copy.
            cv = check_cv(self.cv, y=y, classifier=is_classifier(self))
            if hasattr(cv, 'random_state') and cv.random_state is None:
                cv.random_state = np.random.RandomState()

            fit_params = ({"sample_weight": sample_weight}
                          if sample_weight is not None
                          else None)
            predictions = Parallel(n_jobs=self.n_jobs)(
                delayed(cross_val_predict)(clone(est), X, y, cv=deepcopy(cv),
                                           method=meth, n_jobs=self.n_jobs,
                                           fit_params=fit_params,
                                           verbose=self.verbose)
                for est, meth in zip(all_estimators, self.stack_method_)
                if est != 'drop'
            )

        # Only not None or not 'drop' estimators will be used in transform.
        # Remove the None from the method as well.
        self.stack_method_ = [
            meth for (meth, est) in zip(self.stack_method_, all_estimators)
            if est != 'drop'
        ]

        X_meta = self._concatenate_predictions(X, predictions)
        _fit_single_estimator(self.final_estimator_, X_meta, y,
                              sample_weight=sample_weight)

        return self

    def _transform(self, X):
        """Concatenate and return the predictions of the estimators."""
        check_is_fitted(self)
        predictions = [
            getattr(est, meth)(X)
            for est, meth in zip(self.estimators_, self.stack_method_)
            if est != 'drop'
        ]
        return self._concatenate_predictions(X, predictions)

    @if_delegate_has_method(delegate='final_estimator_')
    def predict(self, X, **predict_params):
        """Predict target for X.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        **predict_params : dict of str -> obj
            Parameters to the `predict` called by the `final_estimator`. Note
            that this may be used to return uncertainties from some estimators
            with `return_std` or `return_cov`. Be aware that it will only
            accounts for uncertainty in the final estimator.

        Returns
        -------
        y_pred : ndarray of shape (n_samples,) or (n_samples, n_output)
            Predicted targets.
        """

        check_is_fitted(self)
        return self.final_estimator_.predict(
            self.transform(X), **predict_params
        )


class StackingClassifier(ClassifierMixin, _BaseStacking):
    """Stack of estimators with a final classifier.

    Stacked generalization consists in stacking the output of individual
    estimator and use a classifier to compute the final prediction. Stacking
    allows to use the strength of each individual estimator by using their
    output as input of a final estimator.

    Note that `estimators_` are fitted on the full `X` while `final_estimator_`
    is trained using cross-validated predictions of the base estimators using
    `cross_val_predict`.

    .. versionadded:: 0.22

    Read more in the :ref:`User Guide <stacking>`.

    Parameters
    ----------
    estimators : list of (str, estimator)
        Base estimators which will be stacked together. Each element of the
        list is defined as a tuple of string (i.e. name) and an estimator
        instance. An estimator can be set to 'drop' using `set_params`.

    final_estimator : estimator, default=None
        A classifier which will be used to combine the base estimators.
        The default classifier is a `LogisticRegression`.

    cv : int, cross-validation generator, iterable, or "prefit", default=None
        Determines the cross-validation splitting strategy used in
        `cross_val_predict` to train `final_estimator`. Possible inputs for
        cv are:

        * None, to use the default 5-fold cross validation,
        * integer, to specify the number of folds in a (Stratified) KFold,
        * An object to be used as a cross-validation generator,
        * An iterable yielding train, test splits.

        For integer/None inputs, if the estimator is a classifier and y is
        either binary or multiclass, `StratifiedKFold` is used. In all other
        cases, `KFold` is used.

        Refer :ref:`User Guide <cross_validation>` for the various
        cross-validation strategies that can be used here.

        If “prefit” is passed, it is assumed that all base_estimators have
        been fitted already.

        .. note::
           A larger number of split will provide no benefits if the number
           of training samples is large enough. Indeed, the training time
           will increase. ``cv`` is not used for model evaluation but for
           prediction.

    stack_method : {'auto', 'predict_proba', 'decision_function', 'predict'}, \
            default='auto'
        Methods called for each base estimator. It can be:

        * if 'auto', it will try to invoke, for each estimator,
          `'predict_proba'`, `'decision_function'` or `'predict'` in that
          order.
        * otherwise, one of `'predict_proba'`, `'decision_function'` or
          `'predict'`. If the method is not implemented by the estimator, it
          will raise an error.

    n_jobs : int, default=None
        The number of jobs to run in parallel all `estimators` `fit`.
        `None` means 1 unless in a `joblib.parallel_backend` context. -1 means
        using all processors. See Glossary for more details.

    passthrough : bool, default=False
        When False, only the predictions of estimators will be used as
        training data for `final_estimator`. When True, the
        `final_estimator` is trained on the predictions as well as the
        original training data.

    verbose : int, default=0
        Verbosity level.

    Attributes
    ----------
    classes_ : ndarray of shape (n_classes,)
        Class labels.

    estimators_ : list of estimators
        The elements of the estimators parameter, having been fitted on the
        training data. If an estimator has been set to `'drop'`, it
        will not appear in `estimators_`.

    named_estimators_ : :class:`~sklearn.utils.Bunch`
        Attribute to access any fitted sub-estimators by name.

    final_estimator_ : estimator
        The classifier which predicts given the output of `estimators_`.

    stack_method_ : list of str
        The method used by each base estimator.

    Notes
    -----
    When `predict_proba` is used by each estimator (i.e. most of the time for
    `stack_method='auto'` or specifically for `stack_method='predict_proba'`),
    The first column predicted by each estimator will be dropped in the case
    of a binary classification problem. Indeed, both feature will be perfectly
    collinear.

    References
    ----------
    .. [1] Wolpert, David H. "Stacked generalization." Neural networks 5.2
       (1992): 241-259.

    Examples
    --------
    >>> from sklearn.datasets import load_iris
    >>> from sklearn.ensemble import RandomForestClassifier
    >>> from sklearn.svm import LinearSVC
    >>> from sklearn.linear_model import LogisticRegression
    >>> from sklearn.preprocessing import StandardScaler
    >>> from sklearn.pipeline import make_pipeline
    >>> from sklearn.ensemble import StackingClassifier
    >>> X, y = load_iris(return_X_y=True)
    >>> estimators = [
    ...     ('rf', RandomForestClassifier(n_estimators=10, random_state=42)),
    ...     ('svr', make_pipeline(StandardScaler(),
    ...                           LinearSVC(random_state=42)))
    ... ]
    >>> clf = StackingClassifier(
    ...     estimators=estimators, final_estimator=LogisticRegression()
    ... )
    >>> from sklearn.model_selection import train_test_split
    >>> X_train, X_test, y_train, y_test = train_test_split(
    ...     X, y, stratify=y, random_state=42
    ... )
    >>> clf.fit(X_train, y_train).score(X_test, y_test)
    0.9...

    """
    def __init__(self, estimators, final_estimator=None, cv=None,
                 stack_method='auto', n_jobs=None, passthrough=False,
                 verbose=0):
        super().__init__(
            estimators=estimators,
            final_estimator=final_estimator,
            cv=cv,
            stack_method=stack_method,
            n_jobs=n_jobs,
            passthrough=passthrough,
            verbose=verbose
        )

    def _validate_final_estimator(self):
        self._clone_final_estimator(default=LogisticRegression())
        if not is_classifier(self.final_estimator_):
            raise ValueError(
                "'final_estimator' parameter should be a classifier. Got {}"
                .format(self.final_estimator_)
            )

    def fit(self, X, y, sample_weight=None):
        """Fit the estimators.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where `n_samples` is the number of samples and
            `n_features` is the number of features.

        y : array-like of shape (n_samples,)
            Target values.

        sample_weight : array-like of shape (n_samples,), default=None
            Sample weights. If None, then samples are equally weighted.
            Note that this is supported only if all underlying estimators
            support sample weights.

        Returns
        -------
        self : object
        """
        check_classification_targets(y)
        self._le = LabelEncoder().fit(y)
        self.classes_ = self._le.classes_
        return super().fit(X, self._le.transform(y), sample_weight)

    @if_delegate_has_method(delegate='final_estimator_')
    def predict(self, X, **predict_params):
        """Predict target for X.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        **predict_params : dict of str -> obj
            Parameters to the `predict` called by the `final_estimator`. Note
            that this may be used to return uncertainties from some estimators
            with `return_std` or `return_cov`. Be aware that it will only
            accounts for uncertainty in the final estimator.

        Returns
        -------
        y_pred : ndarray of shape (n_samples,) or (n_samples, n_output)
            Predicted targets.
        """
        y_pred = super().predict(X, **predict_params)
        return self._le.inverse_transform(y_pred)

    @if_delegate_has_method(delegate='final_estimator_')
    def predict_proba(self, X):
        """Predict class probabilities for X using
        `final_estimator_.predict_proba`.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        Returns
        -------
        probabilities : ndarray of shape (n_samples, n_classes) or \
            list of ndarray of shape (n_output,)
            The class probabilities of the input samples.
        """
        check_is_fitted(self)
        return self.final_estimator_.predict_proba(self.transform(X))

    @if_delegate_has_method(delegate='final_estimator_')
    def decision_function(self, X):
        """Predict decision function for samples in X using
        `final_estimator_.decision_function`.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        Returns
        -------
        decisions : ndarray of shape (n_samples,), (n_samples, n_classes), \
            or (n_samples, n_classes * (n_classes-1) / 2)
            The decision function computed the final estimator.
        """
        check_is_fitted(self)
        return self.final_estimator_.decision_function(self.transform(X))

    def transform(self, X):
        """Return class labels or probabilities for X for each estimator.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where `n_samples` is the number of samples and
            `n_features` is the number of features.

        Returns
        -------
        y_preds : ndarray of shape (n_samples, n_estimators) or \
                (n_samples, n_classes * n_estimators)
            Prediction outputs for each estimator.
        """
        return self._transform(X)


class StackingRegressor(RegressorMixin, _BaseStacking):
    """Stack of estimators with a final regressor.

    Stacked generalization consists in stacking the output of individual
    estimator and use a regressor to compute the final prediction. Stacking
    allows to use the strength of each individual estimator by using their
    output as input of a final estimator.

    Note that `estimators_` are fitted on the full `X` while `final_estimator_`
    is trained using cross-validated predictions of the base estimators using
    `cross_val_predict`.

    .. versionadded:: 0.22

    Read more in the :ref:`User Guide <stacking>`.

    Parameters
    ----------
    estimators : list of (str, estimator)
        Base estimators which will be stacked together. Each element of the
        list is defined as a tuple of string (i.e. name) and an estimator
        instance. An estimator can be set to 'drop' using `set_params`.

    final_estimator : estimator, default=None
        A regressor which will be used to combine the base estimators.
        The default regressor is a `RidgeCV`.

    cv : int, cross-validation generator, iterable, or "prefit", default=None
        Determines the cross-validation splitting strategy used in
        `cross_val_predict` to train `final_estimator`. Possible inputs for
        cv are:

        * None, to use the default 5-fold cross validation,
        * integer, to specify the number of folds in a (Stratified) KFold,
        * An object to be used as a cross-validation generator,
        * An iterable yielding train, test splits.

        For integer/None inputs, if the estimator is a classifier and y is
        either binary or multiclass, `StratifiedKFold` is used. In all other
        cases, `KFold` is used.

        Refer :ref:`User Guide <cross_validation>` for the various
        cross-validation strategies that can be used here.

        If “prefit” is passed, it is assumed that all base_estimators have
        been fitted already.

        .. note::
           A larger number of split will provide no benefits if the number
           of training samples is large enough. Indeed, the training time
           will increase. ``cv`` is not used for model evaluation but for
           prediction.

    n_jobs : int, default=None
        The number of jobs to run in parallel for `fit` of all `estimators`.
        `None` means 1 unless in a `joblib.parallel_backend` context. -1 means
        using all processors. See Glossary for more details.

    passthrough : bool, default=False
        When False, only the predictions of estimators will be used as
        training data for `final_estimator`. When True, the
        `final_estimator` is trained on the predictions as well as the
        original training data.

    verbose : int, default=0
        Verbosity level.

    Attributes
    ----------
    estimators_ : list of estimator
        The elements of the estimators parameter, having been fitted on the
        training data. If an estimator has been set to `'drop'`, it
        will not appear in `estimators_`.

    named_estimators_ : :class:`~sklearn.utils.Bunch`
        Attribute to access any fitted sub-estimators by name.


    final_estimator_ : estimator
        The regressor to stacked the base estimators fitted.

    References
    ----------
    .. [1] Wolpert, David H. "Stacked generalization." Neural networks 5.2
       (1992): 241-259.

    Examples
    --------
    >>> from sklearn.datasets import load_diabetes
    >>> from sklearn.linear_model import RidgeCV
    >>> from sklearn.svm import LinearSVR
    >>> from sklearn.ensemble import RandomForestRegressor
    >>> from sklearn.ensemble import StackingRegressor
    >>> X, y = load_diabetes(return_X_y=True)
    >>> estimators = [
    ...     ('lr', RidgeCV()),
    ...     ('svr', LinearSVR(random_state=42))
    ... ]
    >>> reg = StackingRegressor(
    ...     estimators=estimators,
    ...     final_estimator=RandomForestRegressor(n_estimators=10,
    ...                                           random_state=42)
    ... )
    >>> from sklearn.model_selection import train_test_split
    >>> X_train, X_test, y_train, y_test = train_test_split(
    ...     X, y, random_state=42
    ... )
    >>> reg.fit(X_train, y_train).score(X_test, y_test)
    0.3...

    """
    def __init__(self, estimators, final_estimator=None, cv=None, n_jobs=None,
                 passthrough=False, verbose=0):
        super().__init__(
            estimators=estimators,
            final_estimator=final_estimator,
            cv=cv,
            stack_method="predict",
            n_jobs=n_jobs,
            passthrough=passthrough,
            verbose=verbose
        )

    def _validate_final_estimator(self):
        self._clone_final_estimator(default=RidgeCV())
        if not is_regressor(self.final_estimator_):
            raise ValueError(
                "'final_estimator' parameter should be a regressor. Got {}"
                .format(self.final_estimator_)
            )

    def fit(self, X, y, sample_weight=None):
        """Fit the estimators.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        y : array-like of shape (n_samples,)
            Target values.

        sample_weight : array-like of shape (n_samples,), default=None
            Sample weights. If None, then samples are equally weighted.
            Note that this is supported only if all underlying estimators
            support sample weights.

        Returns
        -------
        self : object
        """
        y = column_or_1d(y, warn=True)
        return super().fit(X, y, sample_weight)

    def transform(self, X):
        """Return the predictions for X for each estimator.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Training vectors, where `n_samples` is the number of samples and
            `n_features` is the number of features.

        Returns
        -------
        y_preds : ndarray of shape (n_samples, n_estimators)
            Prediction outputs for each estimator.
        """
        return self._transform(X)


# Class patch for sklearn to enable Stratified Group K-Fold CV
# Lifted from: https://github.com/scikit-learn/scikit-learn/pull/15239/files
class StratifiedGroupKFold(StratifiedKFold):
    """Stratified K-Folds iterator variant with non-overlapping groups.
    This cross-validation object is a variation of StratifiedKFold that returns
    folds stratified by group class. The folds are made by preserving the
    percentage of groups for each class.
    The same group will not appear in two different folds (the number of
    distinct groups has to be at least equal to the number of folds).
    The difference between GroupKFold and StratifiedGroupKFold is that
    the former attempts to create balanced folds such that the number of
    distinct groups is approximately the same in each fold, whereas
    StratifiedGroupKFold attempts to create folds which preserve the
    percentage of groups for each class.
    Read more in the :ref:`User Guide <cross_validation>`.
    Parameters
    ----------
    n_splits : int, default=5
        Number of folds. Must be at least 2.
    shuffle : bool, default=False
        Whether to shuffle each class's samples before splitting into batches.
        Note that the samples within each split will not be shuffled.
    random_state : int or RandomState instance, default=None
        When `shuffle` is True, `random_state` affects the ordering of the
        indices, which controls the randomness of each fold for each class.
        Otherwise, leave `random_state` as `None`.
        Pass an int for reproducible output across multiple function calls.
        See :term:`Glossary <random_state>`.
    Examples
    --------
    >>> import numpy as np
    >>> from sklearn.model_selection import StratifiedGroupKFold
    >>> X = np.ones((17, 2))
    >>> y = np.array([0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    >>> groups = np.array([1, 1, 2, 2, 3, 3, 3, 4, 5, 5, 5, 5, 6, 6, 7, 8, 8])
    >>> cv = StratifiedGroupKFold(n_splits=3)
    >>> for train_idxs, test_idxs in cv.split(X, y, groups):
    ...     print("TRAIN:", groups[train_idxs])
    ...     print("      ", y[train_idxs])
    ...     print(" TEST:", groups[test_idxs])
    ...     print("      ", y[test_idxs])
    TRAIN: [3 3 3 4 6 6 7 8 8]
           [1 1 1 1 0 0 0 0 0]
     TEST: [1 1 2 2 5 5 5 5]
           [0 0 1 1 0 0 0 0]
    TRAIN: [1 1 2 2 4 5 5 5 5 8 8]
           [0 0 1 1 1 0 0 0 0 0 0]
     TEST: [3 3 3 6 6 7]
           [1 1 1 0 0 0]
    TRAIN: [1 1 2 2 3 3 3 5 5 5 5 6 6 7]
           [0 0 1 1 1 1 1 0 0 0 0 0 0 0]
     TEST: [4 8 8]
           [1 0 0]
    >>> cv = GroupKFold(n_splits=3)
    >>> for train_idxs, test_idxs in cv.split(X, y, groups):
    ...     print("TRAIN:", groups[train_idxs])
    ...     print("      ", y[train_idxs])
    ...     print(" TEST:", groups[test_idxs])
    ...     print("      ", y[test_idxs])
    TRAIN: [2 2 3 3 3 4 6 6 7 8 8]
           [1 1 1 1 1 1 0 0 0 0 0]
     TEST: [1 1 5 5 5 5]
           [0 0 0 0 0 0]
    TRAIN: [1 1 5 5 5 5 6 6 7 8 8]
           [0 0 0 0 0 0 0 0 0 0 0]
     TEST: [2 2 3 3 3 4]
           [1 1 1 1 1 1]
    TRAIN: [1 1 2 2 3 3 3 4 5 5 5 5]
           [0 0 1 1 1 1 1 1 0 0 0 0]
     TEST: [6 6 7 8 8]
           [0 0 0 0 0]
    Notes
    -----
    The implementation is designed to:
    * Generate test sets such that all contain the same distribution of
      group classes, or as close as possible.
    * Be invariant to class label: relabelling ``y = ["Happy", "Sad"]`` to
      ``y = [1, 0]`` should not change the indices generated.
    * Preserve order dependencies in the dataset ordering, when
      ``shuffle=False``: all samples from class k in some test set were
      contiguous in y, or separated in y by samples from classes other than k.
    * Generate test sets where the smallest and largest differ by at most one
      group.
    See also
    --------
    StratifiedKFold: Takes class information into account to build folds which
        retain class distributions (for binary or multiclass classification
        tasks).
    GroupKFold: K-fold iterator variant with non-overlapping groups.
    """

    def __init__(self, n_splits=5, shuffle=False, random_state=None):
        super().__init__(n_splits=n_splits, shuffle=shuffle,
                         random_state=random_state)

    def _iter_test_masks(self, X, y, groups):
        y = check_array(y, ensure_2d=False, dtype=None)
        if groups is None:
            raise ValueError("The 'groups' parameter should not be None.")
        groups = check_array(groups, ensure_2d=False, dtype=None)
        (unique_groups, unique_groups_y), group_indices = np.unique(
            np.stack((groups, y)), axis=1, return_inverse=True)
        n_groups = len(unique_groups)
        if self.n_splits > n_groups:
            raise ValueError("Cannot have number of splits n_splits=%d greater"
                             " than the number of groups: %d."
                             % (self.n_splits, n_groups))
        if unique_groups.shape[0] != np.unique(groups).shape[0]:
            raise ValueError("Members of each group must all be of the same "
                             "class.")
        for group_test in super()._iter_test_masks(X=unique_groups,
                                                   y=unique_groups_y):
            # this is the mask of unique_groups in the partition invert it into
            # a data mask
            yield np.in1d(group_indices, np.where(group_test))
