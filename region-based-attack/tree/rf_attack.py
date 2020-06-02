from typing import List
import gc
from functools import partial
from itertools import product, permutations

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.neighbors import KDTree
from tqdm import tqdm
import joblib
from joblib import Parallel, delayed
import cvxpy as cp

from ..base import AttackModel
from .tree_utils import get_tree_constraints
from ..solvers import solve_lp, solve_qp
from ..knn import sol_sat_constraints, CONSTRAINTTOL

def constraint_list_to_matrix(r):
    rG, rh = [], []
    rC, rd = [], []
    n_dim = len(r) // 2

    for i in range(n_dim):
        temp = np.zeros(n_dim)
        temp[i] = 1
        if np.isclose(r[i], -r[i+n_dim]):
            rC.append(temp)
            rd.append(r[i])
        else:
            temp2 = np.zeros(n_dim)
            temp2[i] = -1
            rG.append(temp)
            rh.append(r[i])
            rG.append(temp2)
            rh.append(r[i+n_dim])

    rG, rh = np.array(rG).astype(np.float32), np.array(rh).astype(np.float32)
    if len(rC) == 0 or len(rd) == 0:
        rC, rd = None, None
    else:
        rC, rd = np.array(rC).astype(np.float32), np.array(rd).astype(np.float32)
    return rG, rh, rC, rd

def union_constraints(G, h):
    assert np.all(np.abs(G).sum(1) == np.ones(len(G)))
    if len(np.shape(G)) <= 1:
        return np.array([]), np.array([])
    n_dim = np.shape(G)[1]

    r = [np.inf for i in range(n_dim*2)]
    for Gi, hi in zip(G, h):
        if Gi.sum() == 1:
            idx = np.where(Gi == 1)[0][0]
            r[idx] = hi if r[idx] is None else min(r[idx], hi)
        elif Gi.sum() == -1:
            idx = np.where(Gi == -1)[0][0] + n_dim
            r[idx] = hi if r[idx] is None else min(r[idx], hi)
        else:
            raise ValueError()
    return r

def tree_instance_constraint(tree_clf, X):
    node_indicator = tree_clf.decision_path(X)
    leave_id = tree_clf.apply(X)
    feature = tree_clf.tree_.feature
    threshold = tree_clf.tree_.threshold
    n_dims = X.shape[1]

    ret = []
    for sample_id in range(len(X)):
        node_index = node_indicator.indices[node_indicator.indptr[sample_id]:
                                            node_indicator.indptr[sample_id + 1]]
        r = [np.inf for i in range(n_dims*2)]
        for node_id in node_index:
            if leave_id[sample_id] == node_id:
                break

            # scikit-learn uses float32 internally
            if (X[sample_id, feature[node_id]].astype(np.float32) <= threshold[node_id]).astype(np.float32):
                #threshold_sign = "<="
                idx = feature[node_id]
                hi = threshold[node_id]
                r[idx] = hi if r[idx] is None else min(r[idx], hi)
            else:
                #threshold_sign = ">"
                idx = feature[node_id] + n_dims
                hi = -threshold[node_id]
                r[idx] = hi if r[idx] is None else min(r[idx], hi)
        ret.append(r)

    return np.asarray(ret).astype(np.float32)

def rev_get_sol_l2(target_x, target_y: int, regions, clf, trnX=None, qp_solver=cp.CVXOPT):
    fet_dim = np.shape(target_x)[0]
    candidates = []
    regions = [constraint_list_to_matrix(r) for r in regions]
    for i, (G, h, C, d) in enumerate(regions):
        #c = np.concatenate((np.zeros(fet_dim), np.ones(1))).reshape((-1, 1))

        Q = 2 * np.eye(fet_dim)
        q = -2 * target_x
        temph = (h - CONSTRAINTTOL).reshape((-1, 1))

        ind = np.where(np.logical_not(np.isinf(temph)))[0]
        tempG = G[ind]
        temph = temph[ind]

        if trnX is None:
            status, sol = solve_qp(Q, q, tempG, temph, len(q), C=C, d=d, solver=qp_solver)
        else:
            status, sol = solve_qp(Q, q, tempG, temph, len(q), C=C, d=d,
                                   init_x=trnX[i].reshape((-1, 1)), solver=qp_solver)

        if status == 'optimal':
            ret = np.array(sol).reshape(-1)

            if clf.predict([ret])[0] != target_y:
                candidates.append(ret - target_x)
            else:
                # a dimension is too close to the boundary region too small
                # just use the traning data as
                if trnX is not None:
                    candidates.append(trnX[i] - target_x)
        elif status == 'infeasible_inaccurate':
            print(status)
            candidates.append(trnX[i] - target_x)
        else:
            print(status)

    norms = np.linalg.norm(candidates, ord=2, axis=1)
    return candidates[norms.argmin()]

def rev_get_sol_linf(target_x, target_y: int, regions, clf,
                     trnX=None, lp_solver=cp.GLPK):
    fet_dim = np.shape(target_x)[0]
    candidates = []
    regions = [constraint_list_to_matrix(r) for r in regions]
    for i, (G, h, C, d) in enumerate(regions):
        c = np.concatenate((np.zeros(fet_dim), np.ones(1))).reshape((-1, 1))

        G2 = np.hstack((np.eye(fet_dim), -np.ones((fet_dim, 1))))
        G3 = np.hstack((-np.eye(fet_dim), -np.ones((fet_dim, 1))))
        G = np.hstack((G, np.zeros((G.shape[0], 1))))
        G = np.vstack((G, G2, G3))
        h = np.concatenate((h, target_x, -target_x))

        temph = (h - CONSTRAINTTOL).reshape((-1, 1))
        ind = np.where(np.logical_not(np.isinf(temph)))[0]
        tempG = G[ind]
        temph = temph[ind]

        if C is not None:
            C = np.hstack((C, np.zeros((C.shape[0], 1))))

        if trnX is None:
            status, sol = solve_lp(c, tempG, temph, C=C, d=d, solver=lp_solver)
        else:
            init_x = np.concatenate((
                trnX[i],
                [np.linalg.norm(trnX[i]-target_x, ord=np.inf)])).reshape((-1, 1))
            status, sol = solve_lp(c, tempG, temph, C=C, d=d, init_x=init_x, solver=lp_solver)

        if status == 'optimal':
            ret = np.array(sol).reshape(-1)[:-1]

            if clf.predict([ret])[0] != target_y:
                candidates.append(ret - target_x)
            else:
                # a dimension is too close to the boundary region too small
                # just use the traning data as
                if trnX is not None:
                    candidates.append(trnX[i] - target_x)
        elif status == 'infeasible_inaccurate':
            candidates.append(trnX[i] - target_x)
        else:
            print(status)

    norms = np.linalg.norm(candidates, ord=np.inf, axis=1)
    return candidates[norms.argmin()]


class RFAttack(AttackModel):
    def __init__(self, trnX: np.ndarray, trny: np.ndarray, clf: RandomForestClassifier,
                norm, method: str = "all", n_searches:int = -1, lp_solver=cp.GLPK,
                qp_solver=cp.CVXOPT, n_jobs: int = 1, verbose=0, random_state=None):
        """Attack on Random forest classifier

        Arguments:
            trnX {ndarray, shape=(n_samples, n_features)} -- Training data
            trny {ndarray, shape=(n_samples)} -- Training label
            clf {RandomForestClassifier} -- The Random Forest classifier
            ord {int} -- Order of the norm for perturbation distance, see numpy.linalg.norm for more information
            method {str} -- 'all' means optimal attack (RBA-Exact), 'rev' means RBA-Approx

        Keyword Arguments:
            n_searches {int} -- number of regions to search, only used when method=='rev' (default: {-1})
            random_state {[type]} -- random seed (default: {None})
        """
        super().__init__()
        paths, constraints = [], []
        self.clf = clf
        self.method = method
        self.n_searches = n_searches
        trnX = trnX.astype(np.float32)
        self.trnX = trnX
        self.trny = trny
        self.random_state = random_state
        self.lp_solver = lp_solver
        self.qp_solver = qp_solver
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.norm = norm
        if self.n_searches != -1:
            self.kd_tree = KDTree(self.trnX)
        else:
            self.kd_tree = None

        if self.method == 'all':
            for tree_clf in clf.estimators_:
                path, constraint = get_tree_constraints(tree_clf)
                paths.append(path)
                constraints.append(constraint)

            n_classes = clf.n_classes_
            n_estimators = len(clf.estimators_)
            self.regions: List[List] = []
            self.region_preds = []
            vacuan_regions = 0

            for res in product(range(n_classes), repeat=n_estimators):
                perm_consts = [list() for _ in range(n_estimators)]

                for i in range(n_estimators):
                    value = clf.estimators_[i].tree_.value
                    path = paths[i]
                    constraint = constraints[i]

                    for p in range(len(path)):
                        if np.argmax(value[path[p][-1]]) == res[i]:
                            perm_consts[i].append(constraint[p])

                for pro in product(*perm_consts):
                    r = union_constraints(
                            np.vstack([j[0] for j in pro]),
                            np.concatenate([j[1] for j in pro]),
                        )
                    G, h, C, d= constraint_list_to_matrix(r)
                    try:
                        status, _ = solve_lp(
                                    np.zeros((len(G[0]))), G, h.reshape(-1, 1),
                                    C=C, d=d, solver=self.lp_solver,
                                )
                        if status == 'optimal':
                            self.region_preds.append(np.argmax(np.bincount(res)))
                            self.regions.append(r)
                        else:
                            vacuan_regions += 1
                    except:
                        vacuan_regions += 1

            if self.verbose > 0:
                print(f"number of regions: {len(self.regions)}")
                print(f"number of vacuan regions: {vacuan_regions}")

        elif self.method == 'rev':
            r = tree_instance_constraint(clf.estimators_[0], trnX)
            for tree_clf in clf.estimators_[1:]:
                t = tree_instance_constraint(tree_clf, trnX)
                r = np.min(np.concatenate(
                    (r[np.newaxis, :], t[np.newaxis, :])), axis=0)
            self.regions = r

            for i in range(len(trnX)):
                G, h, C, d = constraint_list_to_matrix(self.regions[i])
                if C is not None and d is not None:
                    assert np.all(
                            np.logical_and(
                                np.dot(G, trnX[i]) <= (h + CONSTRAINTTOL),
                                np.isclose(np.dot(C, trnX[i]), d),
                            )), i
                else:
                    assert np.all(np.dot(G, trnX[i]) <= (h + CONSTRAINTTOL)), i
                #assert np.all(np.dot(np.vstack(Gss[i]), trnX[i]) <= np.concatenate(hss[i])), i
                #assert np.all(np.dot(G, trnX[i]) <= h), i
        else:
            raise ValueError("Not supported method: %s", self.method)

    def perturb(self, X, y, eps=0.1):
        X = X.astype(np.float32)
        if self.norm == 2:
            get_sol_fn = partial(rev_get_sol_l2, qp_solver=self.qp_solver)
        elif self.norm == np.inf:
            get_sol_fn = partial(rev_get_sol_linf, lp_solver=self.lp_solver)
        else:
            raise ValueError("norm %s not supported", self.norm)

        clf = self.clf
        pred_y = clf.predict(X)
        pred_trn_y = clf.predict(self.trnX)

        if self.method == 'all':
            def _helper(target_x, target_y, pred_yi):
                if pred_yi != target_y:
                    # already incorrect
                    return np.zeros_like(target_x)
                temp_regions = [self.regions[i] for i in range(len(self.regions)) \
                                if self.region_preds[i] != target_y]
                return get_sol_fn(target_x, target_y,
                                  temp_regions, self.clf)

            pert_xs = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
                delayed(_helper)(X[i], y[i], pred_y[i]) for i in range(len(X)))
            pert_X = np.array(pert_xs)

            assert np.all(self.clf.predict(X + pert_X) != y)

        elif self.method == 'rev':
            pert_X = np.zeros_like(X)
            for sample_id in tqdm(range(len(X)), ascii=True, desc="Perturb"):
                if pred_y[sample_id] != y[sample_id]:
                    continue
                target_x, target_y = X[sample_id], y[sample_id]

                if self.n_searches != -1:
                    ind = self.kd_tree.query(
                            target_x.reshape((1, -1)),
                            k=len(self.trnX),
                            return_distance=False)[0]
                    ind = list(filter(lambda x: pred_trn_y[x] != target_y, ind))[:self.n_searches]
                else:
                    ind = list(filter(lambda x: pred_trn_y[x] != target_y, np.arange(len(self.trnX))))
                temp_regions = [self.regions[i] for i in ind]
                pert_x = get_sol_fn(target_x, y[sample_id], temp_regions, self.clf, self.trnX[ind])

                if np.linalg.norm(pert_x) != 0:
                    assert self.clf.predict([X[sample_id] + pert_x])[0] != y[sample_id]
                    pert_X[sample_id, :] = pert_x
                else:
                    raise ValueError("shouldn't happen")
        else:
            raise ValueError("Not supported method %s", self.method)

        self.perts = pert_X
        return pert_X
        #return self._pert_with_eps_constraint(pert_X, eps)
