#!/usr/bin/env python3
"""
Desktop Frame Streamer for ESP32 Display

Captures a selected monitor and streams pixel-level updates to an ESP32
receiver over TCP using a custom binary protocol.
"""

import argparse
import socket
import struct
import time
from typing import Optional, Sequence

import cv2
import mss
import numpy as np
import ctypes

try:
    from Quartz import CGEventCreate, CGEventGetLocation
except Exception:  # noqa: BLE001
    CGEventCreate = None  # type: ignore
    CGEventGetLocation = None  # type: ignore


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

ESP32_DEFAULT_IP = "192.168.1.100"
ESP32_DEFAULT_PORT = 8090
SCREEN_W = 135
SCREEN_H = 240
PIXEL_PROTO_VER = 0x02
RUN_PROTO_VER = 0x01


class FrameStreamClient:
    def __init__(
        self,
        ip: str,
        port: int,
        monitor_index: Optional[int],
        prefer_largest: bool,
        target_fps: float,
        threshold: int,
        full_frame: bool,
        max_updates_per_frame: int,
        rotate_deg: int,
        show_cursor: bool,
    ) -> None:
        self.ip = ip
        self.port = port
        self.monitor_index = monitor_index
        self.prefer_largest = prefer_largest
        self.target_fps = target_fps
        self.threshold = threshold
        self.full_frame = full_frame
        self.max_updates_per_frame = max_updates_per_frame
        self.rotate_deg = rotate_deg
        self.show_cursor = show_cursor

        self.sock: Optional[socket.socket] = None
        self.last_rgb: Optional[np.ndarray] = None
        self.initial_sent: bool = False
        self.frame_id: int = 0
        self.monitor: Optional[dict] = None
        self.sct: Optional[mss.mss] = None
        self.cursor_logged: bool = False
        self.cursor_source: Optional[tuple[str, Optional[ctypes.CDLL]]] = self._detect_cursor_api()

    def _detect_cursor_api(self) -> Optional[tuple[str, Optional[ctypes.CDLL]]]:
        """Try Quartz bindings first, then raw CoreGraphics via ctypes."""
        if CGEventCreate is not None and CGEventGetLocation is not None:
            return ("quartz", None)
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGEventCreate.restype = ctypes.c_void_p
            cg.CGEventGetLocation.restype = CGPoint
            cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
            return ("ctypes", cg)
        except Exception:
            return None

    # --- Connection management ------------------------------------------
    def check_connection(self) -> bool:
        if self.sock:
            return True
        return self.open_connection()

    def open_connection(self, retries: int = 3) -> bool:
        for attempt in range(1, retries + 1):
            try:
                if self.sock:
                    self.sock.close()
                print(f"[CONN] Attempt {attempt}/{retries} -> {self.ip}:{self.port}")
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.settimeout(10)
                self.sock.connect((self.ip, self.port))
                print("[CONN] Connected successfully")
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"[CONN] Failed: {type(exc).__name__}: {exc}")
                if attempt < retries:
                    time.sleep(2)
        return False

    def close_connection(self) -> None:
        if self.sock:
            self.sock.close()
        self.sock = None
        print("[CONN] Connection closed")

    # --- Monitor selection ----------------------------------------------
    @staticmethod
    def _pick_monitor(
        monitors: Sequence[dict],
        monitor_index: Optional[int],
        prefer_largest: bool,
    ) -> Optional[dict]:
        if monitor_index is not None:
            if 1 <= monitor_index < len(monitors):
                return monitors[monitor_index]
            print(f"[MONITOR] Invalid index {monitor_index}; valid range 1..{len(monitors) - 1}")
            return None

        real_monitors = monitors[1:]  # skip virtual bounding box at index 0
        if not real_monitors:
            return None

        if prefer_largest:
            return max(
                real_monitors,
                key=lambda m: m.get("width", 0) * m.get("height", 0),
            )

        # Default: leftmost monitor
        return min(real_monitors, key=lambda m: m.get("left", 0))

    def init_capture(self) -> bool:
        try:
            self.sct = mss.mss()
        except Exception as exc:  # noqa: BLE001
            print(f"[MONITOR] Screen capture init failed: {exc}")
            return False

        monitor = self._pick_monitor(self.sct.monitors, self.monitor_index, self.prefer_largest)
        if not monitor:
            print("[MONITOR] No suitable monitor found")
            return False

        self.monitor = monitor
        print(
            f"[MONITOR] Selected: ({monitor['left']}, {monitor['top']}) "
            f"{monitor['width']}x{monitor['height']}"
        )
        return True

    def capture_screen(self) -> Optional[np.ndarray]:
        if not self.sct or not self.monitor:
            return None
        try:
            img = self.sct.grab(self.monitor)
        except Exception as exc:  # noqa: BLE001
            print(f"[MONITOR] Grab failed: {exc}")
            return None
        return np.array(img)[:, :, :3]  # BGRA -> BGR

    # --- Cursor handling ------------------------------------------------
    def read_cursor_pos(self) -> Optional[tuple[int, int]]:
        if not self.cursor_source:
            if self.show_cursor and not self.cursor_logged:
                print(
                    "[CURSOR] No cursor API available (needs Screen Recording permission "
                    "and Quartz/pyobjc or CoreGraphics ctypes)"
                )
                self.cursor_logged = True
            return None
        api, cg = self.cursor_source
        try:
            if api == "quartz":
                evt = CGEventCreate(None)
                if evt is None:
                    return None
                loc = CGEventGetLocation(evt)
                return int(loc.x), int(loc.y)
            if api == "ctypes" and cg is not None:
                evt = cg.CGEventCreate(None)
                if not evt:
                    return None
                loc = cg.CGEventGetLocation(evt)
                return int(loc.x), int(loc.y)
        except Exception:
            return None
        return None

    def cursor_to_local(self, cursor: tuple[int, int]) -> Optional[tuple[int, int]]:
        if not self.monitor:
            return None
        gx, gy = cursor
        left   = int(self.monitor.get("left", 0))
        top    = int(self.monitor.get("top", 0))
        width  = int(self.monitor.get("width", 0))
        height = int(self.monitor.get("height", 0))

        if width <= 0 or height <= 0:
            return None

        lx = gx - left
        ly = gy - top
        if lx < 0 or ly < 0 or lx >= width or ly >= height:
            if self.show_cursor and not self.cursor_logged:
                print(f"[CURSOR] Outside selected monitor (global {gx},{gy})")
                self.cursor_logged = True
            return None

        if self.show_cursor and not self.cursor_logged:
            print(f"[CURSOR] Mapped global {gx},{gy} -> local {lx},{ly}")
            self.cursor_logged = True
        return int(lx), int(ly)

    # --- Image processing -----------------------------------------------
    @staticmethod
    def convert_to_rgb565(rgb: np.ndarray) -> np.ndarray:
        r = (rgb[:, :, 0] >> 3).astype(np.uint16)
        g = (rgb[:, :, 1] >> 2).astype(np.uint16)
        b = (rgb[:, :, 2] >> 3).astype(np.uint16)
        return (r << 11) | (g << 5) | b

    def render_cursor(self, frame: np.ndarray, pos: tuple[int, int]) -> None:
        px, py = pos
        scale = 1.8
        arrow = np.array(
            [
                (0, 0),
                (0, int(12 * scale)),
                (int(4 * scale), int(10 * scale)),
                (int(8 * scale), int(16 * scale)),
                (int(10 * scale), int(14 * scale)),
                (int(6 * scale), int(8 * scale)),
                (int(12 * scale), int(8 * scale)),
            ],
            dtype=np.int32,
        )
        arrow[:, 0] += px
        arrow[:, 1] += py

        cv2.fillPoly(frame, [arrow], (0, 0, 0))
        cv2.polylines(frame, [arrow], isClosed=True, color=(0, 0, 0), thickness=2, lineType=cv2.LINE_AA)
        cv2.fillPoly(frame, [arrow], (255, 255, 255))
        cv2.polylines(frame, [arrow], isClosed=True, color=(0, 0, 0), thickness=1, lineType=cv2.LINE_AA)

    def scale_and_transform(
        self, frame: np.ndarray, cursor_local: Optional[tuple[int, int]]
    ) -> tuple[np.ndarray, np.ndarray]:
        frame = np.ascontiguousarray(frame)

        if cursor_local:
            cx, cy = cursor_local
            fh, fw, _ = frame.shape
            if 0 <= cx < fw and 0 <= cy < fh:
                self.render_cursor(frame, (cx, cy))

        if self.rotate_deg == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif self.rotate_deg == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif self.rotate_deg == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        scaled = cv2.resize(frame, (SCREEN_W, SCREEN_H))
        rgb = cv2.cvtColor(scaled, cv2.COLOR_BGR2RGB)
        rgb565 = self.convert_to_rgb565(rgb)
        return rgb, rgb565

    # --- Packet encoding ------------------------------------------------
    def create_packets(self, rgb: np.ndarray, rgb565: np.ndarray) -> list[bytes]:
        if self.full_frame or not self.initial_sent or self.last_rgb is None:
            changed = np.ones((SCREEN_H, SCREEN_W), dtype=bool)
        else:
            delta = np.abs(rgb.astype(np.int16) - self.last_rgb.astype(np.int16))
            changed = delta.max(axis=2) > self.threshold

        rows, cols = np.nonzero(changed)
        pixel_colors = rgb565[rows, cols]
        num_changed = len(pixel_colors)

        if num_changed == 0:
            hdr = (
                b"PXUP"
                + bytes([PIXEL_PROTO_VER])
                + struct.pack("<I", self.frame_id)
                + struct.pack("<H", 0)
            )
            self.frame_id += 1
            return [hdr]

        run_pkts = self._encode_run_updates(changed, rgb565)
        px_pkts  = self._encode_pixel_updates(cols, rows, pixel_colors, num_changed)

        run_size = sum(len(p) for p in run_pkts)
        px_size  = sum(len(p) for p in px_pkts)

        if run_size < px_size:
            self.frame_id += len(run_pkts)
            return run_pkts

        self.frame_id += len(px_pkts)
        return px_pkts

    def _encode_pixel_updates(
        self, xs: np.ndarray, ys: np.ndarray, colors: np.ndarray, total: int
    ) -> list[bytes]:
        result: list[bytes] = []
        batch = max(1, self.max_updates_per_frame)
        offset = 0
        while offset < total:
            end = min(offset + batch, total)
            n = end - offset
            hdr = (
                b"PXUP"
                + bytes([PIXEL_PROTO_VER])
                + struct.pack("<I", self.frame_id)
                + struct.pack("<H", n)
            )
            buf = bytearray(hdr)
            write = buf.extend
            for x, y, c in zip(xs[offset:end], ys[offset:end], colors[offset:end]):
                write(struct.pack("<BBH", int(x), int(y), int(c)))
            result.append(bytes(buf))
            offset = end
        return result

    def _encode_run_updates(self, changed: np.ndarray, rgb565: np.ndarray) -> list[bytes]:
        result: list[bytes] = []
        batch = max(1, self.max_updates_per_frame)
        runs: list[tuple[int, int, int, int]] = []

        for y in range(SCREEN_H):
            row = changed[y]
            if not row.any():
                continue
            x = 0
            while x < SCREEN_W:
                if not row[x]:
                    x += 1
                    continue
                start_x = x
                c = int(rgb565[y, start_x])
                x += 1
                while x < SCREEN_W and row[x] and int(rgb565[y, x]) == c:
                    x += 1
                runs.append((y, start_x, x - start_x, c))

        if not runs:
            hdr = (
                b"PXUP"
                + bytes([PIXEL_PROTO_VER])
                + struct.pack("<I", self.frame_id)
                + struct.pack("<H", 0)
            )
            return [hdr]

        offset = 0
        while offset < len(runs):
            end = min(offset + batch, len(runs))
            n = end - offset
            hdr = (
                b"PXUR"
                + bytes([RUN_PROTO_VER])
                + struct.pack("<I", self.frame_id)
                + struct.pack("<H", n)
            )
            buf = bytearray(hdr)
            write = buf.extend
            for y, x0, length, c in runs[offset:end]:
                write(struct.pack("<BBBH", y, x0, length, c))
            result.append(bytes(buf))
            offset = end
        return result

    # --- Main streaming loop --------------------------------------------
    def run(self) -> None:
        if not self.init_capture():
            return
        if not self.check_connection():
            return

        interval = 1.0 / self.target_fps if self.target_fps > 0 else 0.0
        frames = 0
        total_pkts = 0
        total_px = 0
        t0 = time.time()

        print("[STREAM] Streaming started (Ctrl+C to quit)")
        try:
            while True:
                tick = time.time()
                shot = self.capture_screen()
                if shot is None:
                    print("[STREAM] Capture ended")
                    break

                cur_pos = None
                if self.show_cursor:
                    raw_pos = self.read_cursor_pos()
                    if raw_pos:
                        cur_pos = self.cursor_to_local(raw_pos)
                    else:
                        print("[CURSOR] Could not read cursor position")

                rgb, rgb565 = self.scale_and_transform(shot, cur_pos)
                pkts = self.create_packets(rgb, rgb565)
                self.last_rgb = rgb

                if not self.check_connection():
                    print("[SEND] Reconnection failed; stopping")
                    break

                for pkt in pkts:
                    n_updates = struct.unpack_from("<H", pkt, 9)[0]
                    fid = struct.unpack_from("<I", pkt, 5)[0]
                    print(f"[FRAME] id={fid} updates={n_updates}")
                    try:
                        self.sock.sendall(pkt)
                        total_pkts += 1
                        total_px += n_updates
                        if not self.initial_sent:
                            self.initial_sent = True
                    except (BrokenPipeError, ConnectionResetError):
                        print("[SEND] Link lost; reconnecting...")
                        self.close_connection()
                        if not self.check_connection():
                            print("[SEND] Reconnect failed; stopping")
                            break
                        try:
                            self.sock.sendall(pkt)
                            total_pkts += 1
                            total_px += n_updates
                        except Exception as exc:  # noqa: BLE001
                            print(f"[SEND] Retry error: {type(exc).__name__}: {exc}")
                            break
                    except Exception as exc:  # noqa: BLE001
                        print(f"[SEND] Error: {type(exc).__name__}: {exc}")
                        self.close_connection()
                        break
                else:
                    frames += 1
                    now = time.time()
                    dt = now - tick
                    if interval > 0 and dt < interval:
                        time.sleep(interval - dt)

                    if now - t0 >= 1.0:
                        elapsed = now - t0
                        fps = frames / elapsed if elapsed > 0 else 0.0
                        print(
                            f"[STATS] frames:{frames} packets:{total_pkts} "
                            f"pixels:{total_px} fps~{fps:.2f}"
                        )
                        t0 = now
                        frames = 0
                        total_pkts = 0
                        total_px = 0
                    continue
                break
        except KeyboardInterrupt:
            print("\n[STREAM] Stopped by user")
        finally:
            self.close_connection()
            if self.sct:
                self.sct.close()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stream desktop frames as pixel updates to an ESP32 display"
    )
    p.add_argument("--ip", type=str, default=ESP32_DEFAULT_IP, help="ESP32 IP address")
    p.add_argument("--port", type=int, default=ESP32_DEFAULT_PORT, help="TCP port (default 8090)")
    p.add_argument(
        "--monitor-index",
        type=int,
        default=None,
        help="Monitor index (1-based, as reported by mss). Default: leftmost",
    )
    p.add_argument(
        "--prefer-largest",
        action="store_true",
        help="Select the highest-resolution monitor",
    )
    p.add_argument(
        "--target-fps",
        type=float,
        default=15.0,
        help="Maximum capture rate in FPS",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Color difference threshold (0-255) for detecting changed pixels",
    )
    p.add_argument(
        "--full-frame",
        action="store_true",
        help="Transmit all pixels every frame (disable delta encoding)",
    )
    p.add_argument(
        "--max-updates-per-frame",
        type=int,
        default=3000,
        help="Maximum pixel updates per packet slice (default 3000)",
    )
    p.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Rotate the capture to match device orientation",
    )
    p.add_argument(
        "--show-cursor",
        action="store_true",
        help="Overlay cursor on the stream (macOS: requires Quartz/pyobjc)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    client = FrameStreamClient(
        ip=args.ip,
        port=args.port,
        monitor_index=args.monitor_index,
        prefer_largest=args.prefer_largest,
        target_fps=args.target_fps,
        threshold=args.threshold,
        full_frame=args.full_frame,
        max_updates_per_frame=args.max_updates_per_frame,
        rotate_deg=args.rotate,
        show_cursor=args.show_cursor,
    )
    client.run()


if __name__ == "__main__":
    main()

