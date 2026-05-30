"""
util/detect.py — RV1106 RKNN inference via librknnmrt.so ctypes.

rknn_toolkit_lite2 has no armv7l wheel (only aarch64).  Instead we call
librknnmrt.so directly through ctypes, mimicking the RKNNLite API surface
that the rest of the code expects.

Graceful degradation layers:
  1. librknnmrt.so not found       → empty detections, warn once
  2. model .rknn file not found    → empty detections, warn once
  3. rknn_init / inference error   → empty detections, log error
  4. numpy not installed           → empty detections, warn once (unlikely
                                     after production firmware flash)
"""

import ctypes
import logging
import os
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger(__name__)

# ── numpy guard ─────────────────────────────────────────────────────────────
try:
    import numpy as np
    _numpy_ok = True
except ImportError:
    _numpy_ok = False
    log.warning("numpy not installed — RKNN detection disabled. "
                "Flash production firmware to get numpy.")

# ── RKNN C API constants ─────────────────────────────────────────────────────
RKNN_SUCC            = 0
RKNN_ERR_FAIL        = -1
RKNN_TENSOR_NHWC     = 0
RKNN_TENSOR_UINT8    = 4
RKNN_TENSOR_FLOAT16  = 6
RKNN_NPU_CORE_AUTO   = 0


# ── Minimal ctypes structs matching rknn_api.h ───────────────────────────────

class RknnTensorAttr(ctypes.Structure):
    _fields_ = [
        ("index",      ctypes.c_uint32),
        ("n_dims",     ctypes.c_uint32),
        ("dims",       ctypes.c_uint32 * 16),
        ("name",       ctypes.c_char * 256),
        ("n_elems",    ctypes.c_uint32),
        ("size",       ctypes.c_uint32),
        ("fmt",        ctypes.c_int),
        ("type",       ctypes.c_int),
        ("qnt_type",   ctypes.c_int),
        ("fl",         ctypes.c_int8),
        ("zp",         ctypes.c_int32),
        ("scale",      ctypes.c_float),
    ]


class RknnInputOutputNum(ctypes.Structure):
    _fields_ = [
        ("n_input",  ctypes.c_uint32),
        ("n_output", ctypes.c_uint32),
    ]


class RknnInput(ctypes.Structure):
    _fields_ = [
        ("index",    ctypes.c_uint32),
        ("buf",      ctypes.c_void_p),
        ("size",     ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("type",     ctypes.c_int),
        ("fmt",      ctypes.c_int),
    ]


class RknnOutput(ctypes.Structure):
    _fields_ = [
        ("want_float", ctypes.c_uint8),
        ("is_prealloc", ctypes.c_uint8),
        ("index",      ctypes.c_uint32),
        ("buf",        ctypes.c_void_p),
        ("size",       ctypes.c_uint32),
    ]


# ── librknnmrt loader ────────────────────────────────────────────────────────
_LIBRKNN_PATHS = [
    "/usr/lib/librknnmrt.so",
    "/oem/usr/lib/librknnmrt.so",
    "/userdata/librknnmrt.so",
]
_lib = None
_lib_tried = False
_warned_no_lib = False


def _load_lib():
    global _lib, _lib_tried, _warned_no_lib
    if _lib_tried:
        return _lib
    _lib_tried = True
    for p in _LIBRKNN_PATHS:
        if os.path.exists(p):
            try:
                lib = ctypes.CDLL(p)
                # Bind the functions we need
                lib.rknn_init.argtypes = [
                    ctypes.POINTER(ctypes.c_uint64),  # context
                    ctypes.c_void_p,                  # model
                    ctypes.c_uint32,                  # size
                    ctypes.c_uint32,                  # flag
                    ctypes.c_void_p,                  # extend
                ]
                lib.rknn_init.restype = ctypes.c_int

                lib.rknn_destroy.argtypes = [ctypes.c_uint64]
                lib.rknn_destroy.restype = ctypes.c_int

                lib.rknn_query.argtypes = [
                    ctypes.c_uint64, ctypes.c_int,
                    ctypes.c_void_p, ctypes.c_uint32,
                ]
                lib.rknn_query.restype = ctypes.c_int

                lib.rknn_inputs_set.argtypes = [
                    ctypes.c_uint64, ctypes.c_uint32,
                    ctypes.POINTER(RknnInput),
                ]
                lib.rknn_inputs_set.restype = ctypes.c_int

                lib.rknn_run.argtypes = [ctypes.c_uint64, ctypes.c_void_p]
                lib.rknn_run.restype = ctypes.c_int

                lib.rknn_outputs_get.argtypes = [
                    ctypes.c_uint64, ctypes.c_uint32,
                    ctypes.POINTER(RknnOutput), ctypes.c_void_p,
                ]
                lib.rknn_outputs_get.restype = ctypes.c_int

                lib.rknn_outputs_release.argtypes = [
                    ctypes.c_uint64, ctypes.c_uint32,
                    ctypes.POINTER(RknnOutput),
                ]
                lib.rknn_outputs_release.restype = ctypes.c_int

                _lib = lib
                log.info("Loaded RKNN runtime: %s", p)
                return _lib
            except Exception as e:
                log.debug("Failed to load %s: %s", p, e)

    if not _warned_no_lib:
        log.warning("librknnmrt.so not found — RKNN detection disabled. "
                    "Push from SDK: media/iva/iva/librockiva/rockiva-rv1106-Linux/lib/librknnmrt.so")
        _warned_no_lib = True
    return None


# ── RKNNLite-compatible class ─────────────────────────────────────────────────
class RKNNLite:
    """Thin ctypes wrapper around librknnmrt.so with RKNNLite-compatible API."""

    NPU_CORE_AUTO = RKNN_NPU_CORE_AUTO

    def __init__(self):
        self._ctx   = ctypes.c_uint64(0)
        self._lib   = None
        self._n_out = 0

    def load_rknn(self, path: str) -> int:
        lib = _load_lib()
        if lib is None:
            return RKNN_ERR_FAIL
        try:
            data = Path(path).read_bytes()
            buf  = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            ret  = lib.rknn_init(ctypes.byref(self._ctx),
                                 buf, len(data), 0, None)
            if ret != RKNN_SUCC:
                log.error("rknn_init failed: %d", ret)
                return ret
            self._lib = lib
            return RKNN_SUCC
        except Exception as e:
            log.error("load_rknn error: %s", e)
            return RKNN_ERR_FAIL

    def init_runtime(self, core_mask: int = RKNN_NPU_CORE_AUTO) -> int:
        # rknn_init already initialises runtime; nothing extra needed here.
        return RKNN_SUCC if self._lib is not None else RKNN_ERR_FAIL

    def inference(self, inputs: list) -> list:
        """Run inference. inputs is a list of numpy uint8 arrays."""
        if self._lib is None or not inputs:
            return []
        try:
            inp_arr = inputs[0]
            inp_arr = inp_arr.astype(np.uint8)
            raw = inp_arr.tobytes()
            buf  = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw)

            rknn_in = RknnInput()
            rknn_in.index       = 0
            rknn_in.buf         = ctypes.cast(buf, ctypes.c_void_p)
            rknn_in.size        = len(raw)
            rknn_in.pass_through = 0
            rknn_in.type        = RKNN_TENSOR_UINT8
            rknn_in.fmt         = RKNN_TENSOR_NHWC

            ret = self._lib.rknn_inputs_set(self._ctx, 1,
                                            ctypes.byref(rknn_in))
            if ret != RKNN_SUCC:
                log.debug("rknn_inputs_set: %d", ret)
                return []

            ret = self._lib.rknn_run(self._ctx, None)
            if ret != RKNN_SUCC:
                log.debug("rknn_run: %d", ret)
                return []

            # Query output count once
            if self._n_out == 0:
                io_num = RknnInputOutputNum()
                self._lib.rknn_query(self._ctx, 4,  # RKNN_QUERY_IN_OUT_NUM
                                     ctypes.byref(io_num),
                                     ctypes.sizeof(io_num))
                self._n_out = max(1, io_num.n_output)

            n = self._n_out
            outs = (RknnOutput * n)()
            for i in range(n):
                outs[i].want_float   = 1
                outs[i].is_prealloc  = 0
                outs[i].index        = i

            ret = self._lib.rknn_outputs_get(self._ctx, n,
                                             outs, None)
            if ret != RKNN_SUCC:
                log.debug("rknn_outputs_get: %d", ret)
                return []

            results = []
            for i in range(n):
                if outs[i].buf and outs[i].size:
                    arr = np.frombuffer(
                        (ctypes.c_uint8 * outs[i].size).from_address(
                            outs[i].buf),
                        dtype=np.float32
                    ).copy()
                    results.append(arr.reshape(1, -1, 1))

            self._lib.rknn_outputs_release(self._ctx, n, outs)
            return results
        except Exception as e:
            log.debug("inference error: %s", e)
            return []

    def release(self):
        if self._lib and self._ctx.value:
            self._lib.rknn_destroy(self._ctx)
            self._ctx = ctypes.c_uint64(0)

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


# ── Detector (unchanged API surface) ─────────────────────────────────────────
_warned_no_rknn  = False
_warned_no_model = False


class Detector:
    def __init__(self, config: dict):
        det = config.get("detection", {})
        self._vehicle_model_path = det.get("vehicle_model_path", "")
        self._plate_model_path   = det.get("plate_model_path", "")
        self._vehicle_conf = float(det.get("vehicle_confidence", 0.5))
        self._plate_conf   = float(det.get("plate_confidence", 0.4))
        self._input_size   = tuple(det.get("input_size", [640, 640]))
        self._vehicle_rknn = None
        self._plate_rknn   = None
        self._init_done    = False

    def _init_models(self):
        if self._init_done:
            return
        self._init_done = True
        global _warned_no_rknn, _warned_no_model

        if not _numpy_ok:
            return
        if _load_lib() is None:
            return

        for attr, path, label in [
            ("_vehicle_rknn", self._vehicle_model_path, "vehicle"),
            ("_plate_rknn",   self._plate_model_path,   "plate"),
        ]:
            if not path or not Path(path).exists():
                if not _warned_no_model:
                    log.warning("RKNN model not found: %s — "
                                "detection disabled (convert ONNX→RKNN on host)",
                                path or "<empty>")
                    _warned_no_model = True
                continue
            try:
                rknn = RKNNLite()
                if rknn.load_rknn(path) != RKNN_SUCC:
                    log.error("Failed to load RKNN model: %s", path)
                    continue
                if rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) != RKNN_SUCC:
                    log.error("Failed to init RKNN runtime: %s", path)
                    continue
                setattr(self, attr, rknn)
                log.info("Loaded RKNN model: %s", path)
            except Exception as e:
                log.error("Error loading RKNN model %s: %s", path, e)

    def detect(self, image_path: str) -> Tuple[List[dict], List[dict]]:
        self._init_models()
        vehicles: List[dict] = []
        plates:   List[dict] = []

        if self._vehicle_rknn is None and self._plate_rknn is None:
            return vehicles, plates
        if not _numpy_ok:
            return vehicles, plates

        try:
            from PIL import Image
            img        = Image.open(image_path).convert("RGB")
            img_resized = img.resize(self._input_size)
            img_np     = np.array(img_resized, dtype=np.uint8)

            if self._vehicle_rknn:
                vehicles = self._run_inference(
                    self._vehicle_rknn, img_np, img.size, self._vehicle_conf)

            if self._plate_rknn and vehicles:
                for v in vehicles:
                    x, y, w, h = v["bbox"]
                    crop = img.crop((x, y, x + w, y + h)).resize(self._input_size)
                    crop_np = np.array(crop, dtype=np.uint8)
                    p = self._run_inference(
                        self._plate_rknn, crop_np, (w, h), self._plate_conf)
                    plates.extend(p)

        except Exception as e:
            log.warning("Detection failed: %s", e)

        return vehicles, plates

    def _run_inference(self, rknn: RKNNLite, img_np, orig_size,
                       conf_thresh) -> List[dict]:
        results = []
        try:
            outputs = rknn.inference(inputs=[img_np])
            if not outputs:
                return results
            # YOLOv8 output: [1, 84, num_anchors] as float32
            preds  = outputs[0].reshape(84, -1)
            boxes  = preds[:4, :].T
            scores = preds[4:, :].max(axis=0)
            ow, oh = orig_size
            for i, score in enumerate(scores):
                if score < conf_thresh:
                    continue
                cx, cy, bw, bh = boxes[i]
                x = int((cx - bw / 2) * ow)
                y = int((cy - bh / 2) * oh)
                w = int(bw * ow)
                h = int(bh * oh)
                results.append({
                    "bbox":       [max(0, x), max(0, y), max(0, w), max(0, h)],
                    "confidence": float(score),
                })
        except Exception as e:
            log.debug("Inference error: %s", e)
        return results
