#!/usr/bin/env python3.10
"""
CONFRONTO MODELLI sul task forward: statistiche 24/25 -> FM 25/26.

Riusa i dati e le feature di build_stats_rating_2025_26.py (import come libreria)
e confronta, per ruolo e in LOOCV, questi modelli:
  1. ridge            -> baseline lineare regolarizzata (il modello attuale)
  2. ridge_perc       -> ridge sulle feature trasformate in percentile di ruolo
  3. elastic_net      -> lasso+ridge via coordinate descent
  4. gbm              -> gradient boosted tree (stumps, shrinkage), numpy puro
  5. ridge_fvm        -> ridge con FVM come feature aggiuntiva
  6. solo FVM         -> baseline di mercato: predizione = solo il FVM

Riquadro di lettura: se tutti i modelli statistici danno risultati simili e bassi,
il limite e' il segnale (volatilità della FM), non la classe di modello; se il
modello "con FVM" o "solo FVM" dominano, la strada per la stagione successiva e'
un modello ibrido stats+pregio di mercato.

Output:
- data/compare_models_forward.csv
- report a console

Uso:  python3 tools/compare_models_forward.py
"""

import csv
import os

import build_stats_rating_2025_26 as base
import numpy as np
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSV = os.path.join(BASE_DIR, "data", "compare_models_forward.csv")

SCRAPED_CSV = os.path.join(
    BASE_DIR, "data", "serie_a_stats_2024_25", "giocatori_serie_a_2024_25.csv"
)
QUOTAZIONI_XLSX = os.path.join(
    BASE_DIR, "data", "Quotazioni_Fantacalcio_Stagione_2026_27_arricchito.xlsx"
)
base.SCRAPED_CSV = SCRAPED_CSV
base.QUOTAZIONI_XLSX = QUOTAZIONI_XLSX

KNOWN_SDP_ROLES = {"Goalkeeper", "Defender", "Midfielder", "Forward"}


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_quotazioni_with_fvm():
    wb = openpyxl.load_workbook(QUOTAZIONI_XLSX, data_only=True)
    players = []
    for sheet, role in base.XLSX_SHEETS:
        ws = wb[sheet]
        if not isinstance(ws, Worksheet):
            raise TypeError(f"Il foglio {sheet!r} non e' un worksheet dati")
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h).strip() if h else "" for h in rows[1]]
        idx = {name: i for i, name in enumerate(header) if name}
        for row in rows[2:]:
            if not row or not row[idx["Nome"]]:
                continue
            players.append(
                {
                    "R": role,
                    "Nome": str(row[idx["Nome"]]).strip(),
                    "Squadra": str(row[idx["Squadra"]] or "").strip(),
                    "FM": base.to_float(row[idx["FM 25/26"]]),
                    "FVM": base.to_float(row[idx["FVM"]]),
                }
            )
    return players


# --------------------------------------------------------------------------- #
# modelli (numpy)
# --------------------------------------------------------------------------- #


def train_val_split(X, y, w, mean, std, ybar):
    return X, y, w, mean, std, ybar


def fit_ridge_batched(X, y, w, lam):
    """Ridge pesata (equazioni normali, numpy). X gia' standardizzata."""
    n_feat = X.shape[1]
    xtwx = X.T @ (w[:, None] * X) + lam * np.eye(n_feat)
    xtwy = X.T @ (w * y)
    return np.linalg.solve(xtwx, xtwy)


def predict_linear(X, mean, std, ybar, beta):
    z = (X - mean) / std
    return ybar + z @ beta


def elastic_net(X, y, w, lam=0.01, l1_ratio=0.5, iters=500):
    """Coordinate descent su X standardizzata, target centrato pesato."""
    n_feat = X.shape[1]
    beta = np.zeros(n_feat)
    alpha = lam
    for _ in range(iters):
        for j in range(n_feat):
            resid = y - X @ beta + beta[j] * X[:, j]
            rho = np.sum(w * X[:, j] * resid)
            xj2 = np.sum(w * X[:, j] ** 2)
            if xj2 == 0:
                beta[j] = 0.0
                continue
            z = rho / xj2
            threshold = alpha * l1_ratio / xj2
            if z > threshold:
                beta[j] = (z - threshold) / (1 + alpha * (1 - l1_ratio) / xj2)
            elif z < -threshold:
                beta[j] = (z + threshold) / (1 + alpha * (1 - l1_ratio) / xj2)
            else:
                beta[j] = 0.0
            beta[j] += 0.0
    return beta


def gbm_fit(X, y, w, n_trees=120, lr=0.05, depth=2, min_leaf=5, seed=1):
    """Gradient boosting su alberi di regressione (numpy puro)."""
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    if n < 2:
        wtot = np.sum(w)
        return {"f0": safe_float(np.sum(w * y) / wtot), "trees": [], "lr": lr}
    trees = []
    weights = w.copy()
    wtot = weights.sum()
    f = np.full(n, np.sum(weights * y) / wtot)
    for _ in range(n_trees):
        resid = y - f
        k = max(2, min(n, n * 4 // 5))
        try:
            idx = rng.choice(n, size=k, replace=False, p=weights / wtot)
        except ValueError:
            idx = np.arange(k)
        tree = _build_tree(X[idx], resid[idx], weights[idx], depth, min_leaf)
        pred = _tree_predict(tree, X)
        f += lr * pred
        trees.append(tree)
    return {"f0": safe_float(f[0]), "trees": trees, "lr": lr}


def _build_tree(X, y, w, depth, min_leaf):
    """Albero depth<=2 con split a varianza pesata minima."""
    n = len(y)
    if depth == 0 or n < 2 * min_leaf:
        return {"is_leaf": True, "value": safe_float(np.sum(w * y) / np.sum(w))}
    best = None
    best_gain = 0.0
    wsum = np.sum(w)
    base_var = np.sum(w * (y - np.sum(w * y) / wsum) ** 2)
    for j in range(X.shape[1]):
        order = np.argsort(X[:, j])
        xs = X[order, j]
        ys = y[order]
        ws = w[order]
        cum_w = np.cumsum(ws)
        cum_wy = np.cumsum(ws * ys)
        cum_wy2 = np.cumsum(ws * ys**2)
        for i in range(min_leaf, n - min_leaf + 1):
            if xs[i] == xs[i - 1]:
                continue
            wl, wr = cum_w[i - 1], wsum - cum_w[i - 1]
            if wl < 1e-9 or wr < 1e-9:
                continue
            var_l = cum_wy2[i - 1] - cum_wy[i - 1] ** 2 / wl
            var_r = (cum_wy2[-1] - cum_wy2[i - 1]) - (
                cum_wy[-1] - cum_wy[i - 1]
            ) ** 2 / wr
            gain = base_var - (var_l + var_r)
            if gain > best_gain:
                best_gain = gain
                best = (j, (xs[i - 1] + xs[i]) / 2)
    if best is None:
        return {"is_leaf": True, "value": safe_float(np.sum(w * y) / np.sum(w))}
    j, thr = best
    left = X[:, j] <= thr
    return {
        "is_leaf": False,
        "feat": j,
        "thr": thr,
        "left": _build_tree(X[left], y[left], w[left], depth - 1, min_leaf),
        "right": _build_tree(X[~left], y[~left], w[~left], depth - 1, min_leaf),
    }


def _tree_predict(tree, X):
    out = np.empty(X.shape[0])

    def _walk(node, mask):
        if node["is_leaf"]:
            out[mask] = node["value"]
            return
        left = mask & (X[:, node["feat"]] <= node["thr"])
        right = mask & ~left
        _walk(node["left"], left)
        _walk(node["right"], right)

    _walk(tree, np.ones(X.shape[0], dtype=bool))
    return out


def gbm_predict(model, X):
    pred = np.full(X.shape[0], model["f0"])
    for tree in model["trees"]:
        pred += model["lr"] * _tree_predict(tree, X)
    return pred


# --------------------------------------------------------------------------- #
# metriche
# --------------------------------------------------------------------------- #


def metrics(pred, y):
    n = len(y)
    if n < 2:
        return {"pearson": 0.0, "spearman": 0.0, "r2": 0.0, "rmse": 0.0}
    mean_y = np.mean(y)
    ss_tot = np.sum((y - mean_y) ** 2)
    ss_res = np.sum((pred - y) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    pe = np.corrcoef(pred, y)[0, 1] if np.std(pred) > 0 and np.std(y) > 0 else 0.0
    sp = base.spearman(list(pred), list(y))
    return {
        "pearson": safe_float(pe),
        "spearman": safe_float(sp),
        "r2": safe_float(r2),
        "rmse": safe_float((ss_res / n) ** 0.5),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main():
    scraped = base.load_scraped()
    quotazioni = load_quotazioni_with_fvm()
    index = base.build_scraped_index(scraped)
    role_of = {
        r["Id"]: (
            base.SDP_TO_FANTASY.get(r["Ruolo"])
            if r["Ruolo"] in KNOWN_SDP_ROLES
            else None
        )
        for r in scraped
    }
    matched_fm, matched_fvm = {}, {}
    for x in quotazioni:
        if x["FM"] is None or x["FM"] <= 0:
            continue
        sp = base.match_player(
            x["Nome"], base.FANTASY_TO_SDP[x["R"]], x["Squadra"], index
        )
        if sp is None:
            continue
        matched_fm[sp["row"]["Id"]] = x["FM"]
        matched_fvm[sp["row"]["Id"]] = x["FVM"]
        role_of[sp["row"]["Id"]] = x["R"]
    base.SCRAPED, base.MATCHED, base.ROLE_OF = scraped, matched_fm, role_of

    rows_report = []
    print(
        f"{'ruolo':3} {'modello':12} {'n':>3} {'R2':>7} {'Pear':>7} {'Spear':>7} {'RMSE':>7}  note"
    )
    print("-" * 70)
    for role in ("P", "D", "C", "A"):
        _dataset, train = base.build_role_dataset(role, scraped, matched_fm, role_of)
        X0 = np.array([r["vec"] for r in train], dtype=float)
        y = np.array([r["fm"] for r in train], dtype=float)
        minutes = np.array([r["minutes"] for r in train], dtype=float)
        w = np.minimum(1.0, np.sqrt(minutes / base.MIN_MINUTES))
        fvm = np.array(
            [matched_fvm.get(r["row"]["Id"], np.nan) for r in train], dtype=float
        )
        valid_fvm = ~np.isnan(fvm) & (fvm > 0)

        # percentile features (per ruolo)
        def pct_mat(X):
            n = len(X)
            out = np.zeros_like(X, dtype=float)
            for j in range(X.shape[1]):
                col = X[:, j]
                order = np.argsort(col)
                ranks = np.empty(n, dtype=float)
                k = 0
                while k < n:
                    m = k
                    while m + 1 < n and col[order[m + 1]] == col[order[k]]:
                        m += 1
                    avg = (k + m) / 2 + 1
                    ranks[order[k : m + 1]] = avg
                    k = m + 1
                out[:, j] = (ranks - 1) / max(n - 1, 1)
            return out

        Xp = pct_mat(X0)

        models_cfg = []
        for name, X, use_out in (
            ("ridge", X0, True),
            ("ridge_perc", Xp, True),
            ("elastic_net", X0, True),
            ("gbm", X0, True),
            ("ridge_fvm", None, False),
            ("solo_FVM", None, False),
        ):
            if name in ("ridge_fvm", "solo_FVM"):
                if np.sum(valid_fvm) < 10:
                    continue
                x0s = X0.std(axis=0)
                x0s[x0s == 0] = 1.0
                fvm_sd = fvm[valid_fvm].std()
                if fvm_sd == 0:
                    fvm_sd = 1.0
                Xh = np.column_stack(
                    [
                        (X0 - X0.mean(axis=0)) / x0s,
                        (fvm - fvm[valid_fvm].mean()) / fvm_sd,
                    ]
                )
                models_cfg.append((name, Xh, valid_fvm, True))
            else:
                models_cfg.append((name, X, np.ones(len(train), dtype=bool), use_out))

        for name, X, mask, _ in models_cfg:
            idx_list = np.where(mask)[0]
            n = len(idx_list)
            preds = np.empty(n)
            for i in range(n):
                te = idx_list[i]
                tr = idx_list[idx_list != te]
                Xtr, ytr, wtr = X[tr], y[tr], w[tr]
                ybar_tr = (
                    safe_float(np.sum(wtr * ytr) / np.sum(wtr)) if np.sum(wtr) else 0.0
                )
                if name == "gbm":
                    mfit = gbm_fit(Xtr, ytr, wtr, seed=1)
                    preds[i] = gbm_predict(mfit, X[te : te + 1])[0]
                elif name == "elastic_net":
                    mean, std = Xtr.mean(axis=0), Xtr.std(axis=0)
                    std[std == 0] = 1.0
                    Ztr = (Xtr - mean) / std
                    beta = elastic_net(Ztr, ytr - ybar_tr, wtr)
                    zte = (X[te] - mean) / std
                    preds[i] = ybar_tr + safe_float(zte @ beta)
                elif name == "solo_FVM":
                    # regressione lineare pesata 1-d su FVM
                    x = (X[tr, -1] - X[tr, -1].mean()) / (X[tr, -1].std() or 1.0)
                    zt = (X[te, -1] - X[tr, -1].mean()) / (X[tr, -1].std() or 1.0)
                    xw = np.sum(wtr * x * x) + 1e-9
                    beta1 = np.sum(wtr * x * (ytr - ybar_tr)) / xw
                    preds[i] = ybar_tr + safe_float(beta1 * zt)
                else:  # ridge / ridge_perc / ridge_fvm
                    mean, std = Xtr.mean(axis=0), Xtr.std(axis=0)
                    std[std == 0] = 1.0
                    Ztr = (Xtr - mean) / std
                    beta = fit_ridge_batched(
                        Ztr, ytr - ybar_tr, wtr, base.LAMBDA_BY_ROLE[role]
                    )
                    zte = (X[te] - mean) / std
                    preds[i] = ybar_tr + safe_float(zte @ beta)
            m = metrics(preds, y[idx_list])
            rows_report.append({"ruolo": role, "modello": name, "n": n, **m})
            print(
                f"{role:3} {name:12} {n:3} {m['r2']:7.3f} {m['pearson']:7.3f} {m['spearman']:7.3f} {m['rmse']:7.3f}"
            )

    try:
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "ruolo",
                    "modello",
                    "n",
                    "r2",
                    "pearson",
                    "spearman",
                    "rmse",
                ],
            )
            writer.writeheader()
            writer.writerows(rows_report)
    except OSError as err:
        raise RuntimeError(f"Impossibile scrivere {OUT_CSV}: {err}") from err
    print(f"\nRisultati: {os.path.relpath(OUT_CSV, BASE_DIR)}")


if __name__ == "__main__":
    main()
