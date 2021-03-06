from scipy import optimize as _optimize
import numpy as _np
from tqdm import tqdm as _tqdm
import numba as _numba
import multiprocessing as _multiprocessing
from concurrent import futures as _futures
from . import postprocess as _postprocess


@_numba.jit(nopython=True, nogil=True)
def _gaussian(mu, sigma, grid):
    norm = 0.3989422804014327 / sigma
    return norm * _np.exp(-0.5 * ((grid - mu) / sigma)**2)


'''
def integrated_gaussian(mu, sigma, grid):
    norm = 0.70710678118654757 / sigma   # sq_norm = sqrt(0.5/sigma**2)
    return 0.5 * (erf((grid - mu + 0.5) * norm) - erf((grid - mu - 0.5) * norm))
'''


@_numba.jit(nopython=True, nogil=True)
def _sum_and_center_of_mass(spot, size):
    x = 0.0
    y = 0.0
    _sum_ = 0.0
    for i in range(size):
        for j in range(size):
            x += spot[i, j] * i
            y += spot[i, j] * j
            _sum_ += spot[i, j]
    x /= _sum_
    y /= _sum_
    return _sum_, y, x


@_numba.jit(nopython=True, nogil=True)
def _initial_sigmas(spot, y, x, sum, size):
    sum_deviation_y = 0.0
    sum_deviation_x = 0.0
    for i in range(size):
        for j in range(size):
            sum_deviation_y += spot[i, j] * (i - y)**2
            sum_deviation_x += spot[i, j] * (j - x)**2
    sy = _np.sqrt(sum_deviation_y / sum)
    sx = _np.sqrt(sum_deviation_x / sum)
    return sy, sx


@_numba.jit(nopython=True, nogil=True)
def _initial_parameters(spot, size, size_half):
    theta = _np.zeros(6, dtype=_np.float32)
    theta[3] = _np.min(spot)
    spot_without_bg = spot - theta[3]
    sum, theta[1], theta[0] = _sum_and_center_of_mass(spot_without_bg, size)
    theta[2] = _np.maximum(1.0, sum)
    theta[5], theta[4] = _initial_sigmas(spot-theta[3], theta[1], theta[0], sum, size)
    theta[0:2] -= size_half
    return theta


@_numba.jit(nopython=True, nogil=True)
def _outer(a, b, size, model, n, bg):
    for i in range(size):
        for j in range(size):
            model[i, j] = n * a[i] * b[j] + bg


@_numba.jit(nopython=True, nogil=True)
def _compute_model(theta, grid, size, model_x, model_y, model):
    model_x[:] = _gaussian(theta[0], theta[4], grid)    # sx and sy are wrong with integrated gaussian
    model_y[:] = _gaussian(theta[1], theta[5], grid)
    _outer(model_y, model_x, size, model, theta[2], theta[3])
    return model


@_numba.jit(nopython=True, nogil=True)
def _compute_residuals(theta, spot, grid, size, model_x, model_y, model, residuals):
    _compute_model(theta, grid, size, model_x, model_y, model)
    residuals[:, :] = spot - model
    return residuals.flatten()


def fit_spot(spot):
    size = spot.shape[0]
    size_half = int(size / 2)
    grid = _np.arange(-size_half, size_half + 1, dtype=_np.float32)
    model_x = _np.empty(size, dtype=_np.float32)
    model_y = _np.empty(size, dtype=_np.float32)
    model = _np.empty((size, size), dtype=_np.float32)
    residuals = _np.empty((size, size), dtype=_np.float32)
    # theta is [x, y, photons, bg, sx, sy]
    theta0 = _initial_parameters(spot, size, size_half)
    args = (spot, grid, size, model_x, model_y, model, residuals)
    result = _optimize.leastsq(_compute_residuals, theta0, args=args, ftol=1e-2, xtol=1e-2)   # leastsq is much faster than least_squares
    '''
    model = compute_model(result[0], grid, size, model_x, model_y, model)
    plt.figure()
    plt.subplot(121)
    plt.imshow(spot, interpolation='none')
    plt.subplot(122)
    plt.imshow(model, interpolation='none')
    plt.colorbar()
    plt.show()
    '''
    return result[0]


def fit_spots(spots):
    theta = _np.empty((len(spots), 6), dtype=_np.float32)
    theta.fill(_np.nan)
    for i, spot in enumerate(spots):
        theta[i] = fit_spot(spot)
    return theta


def fit_spots_parallel(spots, async=False):
    n_workers = max(1, int(0.75 * _multiprocessing.cpu_count()))
    n_spots = len(spots)
    n_tasks = 100 * n_workers
    spots_per_task = [int(n_spots / n_tasks + 1) if _ < n_spots % n_tasks else int(n_spots / n_tasks) for _ in range(n_tasks)]
    start_indices = _np.cumsum([0] + spots_per_task[:-1])
    fs = []
    executor = _futures.ProcessPoolExecutor(n_workers)
    for i, n_spots_task in zip(start_indices, spots_per_task):
        fs.append(executor.submit(fit_spots, spots[i:i+n_spots_task]))
    if async:
        return fs
    with _tqdm(total=n_tasks, unit='task') as progress_bar:
        for f in _futures.as_completed(fs):
            progress_bar.update()
    return fits_from_futures(fs)


def fits_from_futures(futures):
    theta = [_.result() for _ in futures]
    return _np.vstack(theta)


def locs_from_fits(identifications, theta, box, em):
    # box_offset = int(box/2)
    x = theta[:, 0] + identifications.x     # - box_offset
    y = theta[:, 1] + identifications.y     # - box_offset
    lpx = _postprocess.localization_precision(theta[:, 2], theta[:, 4], theta[:, 3], em=em)
    lpy = _postprocess.localization_precision(theta[:, 2], theta[:, 5], theta[:, 3], em=em)
    a = _np.maximum(theta[:, 4], theta[:, 5])
    b = _np.minimum(theta[:, 4], theta[:, 5])
    ellipticity = (a - b) / a
    locs = _np.rec.array((identifications.frame, x, y,
                          theta[:, 2], theta[:, 4], theta[:, 5],
                          theta[:, 3], lpx, lpy, ellipticity,
                          identifications.net_gradient),
                         dtype=[('frame', 'u4'), ('x', 'f4'), ('y', 'f4'),
                                ('photons', 'f4'), ('sx', 'f4'), ('sy', 'f4'),
                                ('bg', 'f4'), ('lpx', 'f4'), ('lpy', 'f4'),
                                ('ellipticity', 'f4'), ('net_gradient', 'f4')])
    locs.sort(kind='mergesort', order='frame')
    return locs
