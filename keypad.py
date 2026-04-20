"""
SPI Raw Capture Viewer + Keypad
===============================
STM32 streams raw captures, Python listens and decodes.
Also supports keypad button press/release commands.

Requirements:
    pip install pyserial Pillow

Button mapping (from schematic):
    Key    | STM Pin | BTN name
    -------|---------|----------
    #      | PA0     | BTN_1
    0      | PA1     | BTN_2
    7      | PA2     | BTN_3
    8      | PA3     | BTN_4
    9      | PA8     | BTN_5
    ENTER  | PA9     | BTN_6
    4      | PA10    | BTN_7
    5      | PA11    | DISABLED (USB DM)
    6      | PA12    | DISABLED (USB DP)
    DOWN   | PA15    | BTN_10
    1      | PB3     | BTN_11
    2      | PB4     | BTN_12
    3      | PB5     | BTN_13
    UP     | PB6     | BTN_14
"""

import time
import struct
import threading
import hashlib
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox
from PIL import Image, ImageTk
import serial
import serial.tools.list_ports


BAUD = 115200
READY_TIMEOUT = 10
DISPLAY_SCALE = 2

RAW_MAGIC = 0x31574152
RAW_HEADER_SIZE = 16

DECODER_FILTER = "filter_array"
DECODER_PAGE = "page_headers"
DECODER_AUTO = "auto"


def filter_array(data: bytes):
    payload = [0] * 2048

    if len(data) < (64 * 132 + 2):
        raise ValueError(f"Not enough data for filter_array path: got {len(data)} bytes")

    for row in range(64):
        for i in range(128):
            index = i + row * 132 + 2
            value = data[index]

            bit1 = ((value & 0x10) >> 4) << ((i % 4) * 2)
            bit2 = (value & 0x01) << ((i % 4) * 2 + 1)

            payload[i // 4 + row * 32] |= bit1 | bit2

    return bytes(payload)


def display_to_image(disp: bytes):
    if len(disp) != 64 * 32:
        raise ValueError(f"Expected 2048 bytes (64x32 display), got {len(disp)}")

    img = Image.new("1", (256, 64))
    pixels = img.load()

    for row in range(64):
        row_data = disp[row * 32:(row * 32 + 32)]
        row_bits = "".join(f"{i:08b}"[::-1] for i in row_data)
        for i in range(len(row_bits)):
            pixel_val = (row_bits[i] == "1")
            pixels[i, row] = 255 if pixel_val else 0

    return img.convert("L")


def make_image_from_screen(screen: bytes):
    filtered_data = filter_array(screen)
    return display_to_image(filtered_data)


def is_page_header(buf, i):
    if i + 2 >= len(buf):
        return False
    cmd = buf[i]
    if not (0xB0 <= cmd <= 0xBF):
        return False
    col_low  = buf[i + 1]
    col_high = buf[i + 2]
    return (0x00 <= col_low <= 0x0F) and (0x10 <= col_high <= 0x1F)


def make_image_from_screen_cenconX(data: bytes):
    pages = {}
    max_col = -1
    min_col = 1_000_000
    max_page = -1

    i = 0
    while i < len(data):
        if is_page_header(data, i):
            page      = data[i] & 0x0F
            col_start = ((data[i + 2] & 0x0F) << 4) | (data[i + 1] & 0x0F)
            i += 3
            col = col_start

            while i < len(data) and not is_page_header(data, i):
                byte = data[i]
                if page not in pages:
                    pages[page] = {}
                pages[page][col] = byte
                if col > max_col:  max_col  = col
                if col < min_col:  min_col  = col
                if page > max_page: max_page = page
                col += 1
                i   += 1
        else:
            i += 1

    if max_page < 0 or max_col < 0:
        raise RuntimeError("No recognizable page data found.")

    col_offset = 0 if min_col == 0 else min_col
    width  = (max_col - col_offset) + 1
    height = (max_page + 1) * 8

    img = Image.new("1", (width, height), 1)
    px  = img.load()

    for page, cols in pages.items():
        y_base = page * 8
        for col, byte in cols.items():
            x = col - col_offset
            if 0 <= x < width:
                for bit in range(8):
                    on = (byte >> bit) & 1
                    y  = y_base + bit
                    if 0 <= y < height:
                        px[x, y] = 1 if on else 0

    return img.convert("L")


def decode_raw_capture(raw: bytes, mode: str):
    errors = []

    if mode in (DECODER_FILTER, DECODER_AUTO):
        try:
            return make_image_from_screen(raw), DECODER_FILTER
        except Exception as e:
            errors.append(f"{DECODER_FILTER}: {e}")

    if mode in (DECODER_PAGE, DECODER_AUTO):
        try:
            return make_image_from_screen_cenconX(raw), DECODER_PAGE
        except Exception as e:
            errors.append(f"{DECODER_PAGE}: {e}")

    raise RuntimeError(" | ".join(errors) if errors else "No decoder succeeded")


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

class SerialWorker:
    def __init__(self, on_log, on_image, on_fw_status):
        self.on_log       = on_log
        self.on_image     = on_image
        self.on_fw_status = on_fw_status

        self.ser            = None
        self.running        = False
        self.stream_enabled = False

        self._rx_buf       = bytearray()
        self._mode         = "text"
        self._raw_expected = 0

        self.last_img          = None
        self.last_raw          = b""
        self.last_decoder_used = None
        self.last_payload_hash = None

        self.decoder_mode = DECODER_AUTO

    def connect(self, port):
        self.ser              = serial.Serial()
        self.ser.port         = port
        self.ser.baudrate     = BAUD
        self.ser.timeout      = 0.1
        self.ser.write_timeout = 1
        self.ser.dtr          = False
        self.ser.rts          = False
        self.ser.open()

        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, cmd):
        if self.ser and self.ser.is_open:
            self.ser.write((cmd + "\n").encode("utf-8"))

    def set_decoder_mode(self, mode):
        self.decoder_mode = mode
        self.on_log(f"Decoder mode set to: {mode}")

    def start_stream(self):
        self.last_payload_hash = None
        self.stream_enabled    = True
        self.send("STREAM_ON")
        self.on_log("▶ Stream requested")

    def stop_stream(self):
        self.stream_enabled = False
        self.send("STREAM_OFF")
        self.on_log("⏹ Stream stop requested")

    def _run(self):
        self.on_log(f"Waiting up to {READY_TIMEOUT}s for STM32 response...")

        start = time.time()
        while self.running and (time.time() - start < READY_TIMEOUT):
            chunk = self.ser.read(256)
            if chunk:
                self._rx_buf.extend(chunk)
                self._drain_boot_lines()
                if b"READY" in self._rx_buf or b"ARMED" in self._rx_buf:
                    self.on_log("STM32 responded ✓")
                    break
        else:
            self.on_log("No startup heartbeat seen. Continuing anyway.")

        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        self._rx_buf.clear()
        self.send("STATUS")

        while self.running:
            try:
                chunk = self.ser.read(512)
                if not chunk:
                    continue
                self._rx_buf.extend(chunk)
                self._process_stream()
            except Exception as e:
                if self.running:
                    self.on_log(f"Serial error: {e}")
                break

    def _drain_boot_lines(self):
        while b"\n" in self._rx_buf:
            line, rest        = self._rx_buf.split(b"\n", 1)
            self._rx_buf[:]   = rest
            text              = line.decode("utf-8", errors="ignore").strip()
            if text:
                self.on_log(f"[BOOT] {text}")

    def _process_stream(self):
        while self.running:
            if self._mode == "raw":
                if len(self._rx_buf) < self._raw_expected:
                    return
                raw = bytes(self._rx_buf[:self._raw_expected])
                del self._rx_buf[:self._raw_expected]
                self._finish_raw_capture(raw)
                self._mode = "text"
                continue

            if len(self._rx_buf) >= RAW_HEADER_SIZE:
                maybe_magic = struct.unpack_from("<I", self._rx_buf, 0)[0]
                if maybe_magic == RAW_MAGIC:
                    header = bytes(self._rx_buf[:RAW_HEADER_SIZE])
                    del self._rx_buf[:RAW_HEADER_SIZE]
                    self._start_raw_capture_from_header(header)
                    continue

            if b"\n" not in self._rx_buf:
                return

            line, rest        = self._rx_buf.split(b"\n", 1)
            self._rx_buf[:]   = rest
            text              = line.decode("utf-8", errors="ignore").strip()
            if text:
                self._handle_text_line(text)

    def _start_raw_capture_from_header(self, header):
        magic, length, flags, reserved, sequence = struct.unpack("<IIHHI", header)
        if magic != RAW_MAGIC:
            self.on_log("Bad RAW1 magic")
            return
        self._raw_expected = length
        self._mode         = "raw"
        self.on_log(f"Raw capture header: bytes={length}, seq={sequence}, flags=0x{flags:04X}")
        self.on_fw_status(f"DOWNLOADING RAW ({length} bytes)")

    def _finish_raw_capture(self, raw: bytes):
        self.last_raw    = raw
        payload_hash     = hashlib.sha1(raw).hexdigest()

        if payload_hash == self.last_payload_hash:
            self.on_log("Duplicate frame ignored")
            self.on_fw_status("DUPLICATE IGNORED")
            return

        self.last_payload_hash = payload_hash
        self.on_log(f"Raw payload received: {len(raw)} bytes")

        try:
            img, decoder_used      = decode_raw_capture(raw, self.decoder_mode)
            self.last_img          = img
            self.last_decoder_used = decoder_used
            self.on_image(img)
            self.on_fw_status(f"IMAGE READY ({decoder_used})")
            self.on_log(f"Decode successful using: {decoder_used}")
        except Exception as e:
            self.last_img          = None
            self.last_decoder_used = None
            self.on_fw_status("DECODE ERROR")
            self.on_log(f"Decode error: {e}")

    def _handle_text_line(self, line):
        if line.startswith("FRAME_READY"):
            self.on_log(f"✓ {line}")
            self.on_fw_status(line)
            return
        if line.startswith("CAPTURE_START"):
            self.on_log(line)
            self.on_fw_status("CAPTURING...")
            return
        if line.startswith("VERIFY"):
            self.on_log(f"[VERIFY] {line}")
            return
        if line.startswith("PREVIEW"):
            self.on_log(f"[PREVIEW] {line}")
            return
        if line.startswith("FRAME_SENT"):
            return
        if line.startswith("STATUS"):
            self.on_log(f"[STATUS] {line}")
            self.on_fw_status(line.replace("STATUS ", ""))
            return
        if line.startswith("ACK:STREAM_ON"):
            self.on_log("Streaming enabled ✓")
            self.on_fw_status("STREAMING")
            return
        if line.startswith("ACK:STREAM_OFF"):
            self.on_log("Streaming disabled")
            self.on_fw_status("STREAM STOPPED")
            return
        if line == "ACK":
            return
        if line.startswith("ARMED"):
            if not self.stream_enabled:
                self.on_log(line)
                self.on_fw_status("ARMED")
            return
        if line.startswith("CAPTURE_ABORT"):
            self.on_log(f"⚠ {line}")
            self.on_fw_status("CAPTURE ABORT")
            return
        if line == "PONG":
            self.on_log("PONG ✓")
            return
        if line.startswith("ERR:") or line.startswith("ERR"):
            self.on_log(f"⚠ {line}")
            return
        if line.startswith("READY"):
            return
        self.on_log(f"[STM32] {line}")

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class KeypadWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("SPI Raw Capture Viewer + Keypad")
        

        self.worker         = None
        self._photo         = None
        self.current_width  = 256
        self.current_height = 64

        self._build()
        self._refresh_ports()
        self._placeholder()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # ── top bar ────────────────────────────────────────────────────────
        bar = tk.Frame(self, bg="#e0e0e0", pady=6, padx=10)
        bar.pack(fill=tk.X)

        tk.Label(bar, text="Port:", bg="#e0e0e0").pack(side=tk.LEFT)

        self.port_var   = tk.StringVar()
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, width=28)
        self.port_combo.pack(side=tk.LEFT, padx=4)

        ttk.Button(bar, text="↻", width=3, command=self._refresh_ports).pack(side=tk.LEFT)

        self.connect_btn = ttk.Button(bar, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side=tk.LEFT, padx=6)

        self.conn_status = tk.Label(bar, text="Not connected", bg="#e0e0e0", fg="#666")
        self.conn_status.pack(side=tk.LEFT, padx=8)

        tk.Label(bar, text="Decoder:", bg="#e0e0e0").pack(side=tk.LEFT, padx=(16, 4))
        self.decoder_var   = tk.StringVar(value=DECODER_AUTO)
        self.decoder_combo = ttk.Combobox(
            bar,
            textvariable=self.decoder_var,
            width=14,
            state="readonly",
            values=[DECODER_AUTO, DECODER_FILTER, DECODER_PAGE],
        )
        self.decoder_combo.pack(side=tk.LEFT)
        self.decoder_combo.bind("<<ComboboxSelected>>", self._decoder_changed)

        # ── main area ──────────────────────────────────────────────────────
        main = tk.Frame(self, bg="#f0f0f0")
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        # left column
        left = tk.Frame(main, bg="#f0f0f0")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        tk.Label(left, text="Screen Preview", bg="#f0f0f0",
                 font=("Arial", 11, "bold")).pack(anchor=tk.W)

        self.canvas = tk.Canvas(
            left,
            width=self.current_width  * DISPLAY_SCALE,
            height=self.current_height * DISPLAY_SCALE,
            bg="#000",
            highlightthickness=2,
            highlightbackground="#888"
        )
        self.canvas.pack()

        self.fw_status_label = tk.Label(
            left, text="Status: IDLE",
            bg="#f0f0f0", fg="#444",
            font=("Courier New", 9)
        )
        self.fw_status_label.pack(pady=(4, 8))

        btn_style = {
            "width": 22, "height": 2,
            "font": ("Arial", 11, "bold"),
            "relief": tk.RAISED, "cursor": "hand2"
        }

        tk.Button(left, text="▶ Start Stream",  bg="#2196F3", fg="white",
                  command=self._start_stream, **btn_style).pack(fill=tk.X, pady=3)
        tk.Button(left, text="⏹ Stop Stream",   bg="#FF9800", fg="white",
                  command=self._stop_stream,  **btn_style).pack(fill=tk.X, pady=3)
        tk.Button(left, text="❓ Status",        bg="#607D8B", fg="white",
                  command=self._status,       **btn_style).pack(fill=tk.X, pady=3)
        tk.Button(left, text="💾 Save PNG",      bg="#9C27B0", fg="white",
                  command=self._save_png,     **btn_style).pack(fill=tk.X, pady=3)
        tk.Button(left, text="💾 Save RAW",      bg="#795548", fg="white",
                  command=self._save_raw,     **btn_style).pack(fill=tk.X, pady=3)

        keypad = tk.LabelFrame(left, text="Keypad", bg="#f0f0f0", padx=8, pady=8)
        keypad.pack(fill=tk.X, pady=(12, 0))
        self._build_keypad(keypad)

        # right column – log
        right = tk.Frame(main, bg="#f0f0f0")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(right, text="Log", bg="#f0f0f0",
                 font=("Arial", 11, "bold")).pack(anchor=tk.W)

        self.log = scrolledtext.ScrolledText(
            right,
            font=("Courier New", 9),
            bg="#111", fg="#00ff88",
            state=tk.DISABLED,
            wrap=tk.WORD,
            height=30
        )
        self.log.pack(fill=tk.BOTH, expand=True)
        ttk.Button(right, text="Clear Log", command=self._clear_log).pack(anchor=tk.W, pady=4)

    def _build_keypad(self, parent):
        # ── Layout matches physical keypad ──
        #
        # Each entry: (display_label, btn_name, enabled)
        #
        # Correct mapping from schematic:
        #   PA0  = #(pound) = BTN_1   OK
        #   PA1  = 0        = BTN_2   OK
        #   PA2  = 7        = BTN_3   OK
        #   PA3  = 8        = DISABLED (GPIO_Input — DC/RS line)
        #   PA8  = 9        = BTN_5   OK
        #   PA9  = ENTER    = BTN_6   OK
        #   PA10 = 4        = BTN_7   OK
        #   PA11 = 5        = DISABLED (USB DM)
        #   PA12 = 6        = DISABLED (USB DP)
        #   PA15 = DOWN     = BTN_10  OK
        #   PB3  = 1        = BTN_11  OK
        #   PB4  = 2        = BTN_12  OK
        #   PB5  = 3        = BTN_13  OK
        #   PB6  = UP       = BTN_14  OK
        #
        # Grid layout (row, col):
        #   row0: 1,    2,    3,    [empty]
        #   row1: 4,    5*,   6*,   UP
        #   row2: 7,    8*,   9,    DOWN
        #   row3: #,    0,    [empty], ENTER
        #
        # * disabled: 5=USB, 6=USB, 8=DC line input

        keys = [
            # row 0
            [("1",     "BTN_11", True),
             ("2",     "BTN_12", True),
             ("3",     "BTN_13", True),
             ("",      None,     False)],
            # row 1
            [("4",     "BTN_7",  True),
             ("5",     "BTN_8",  True),
             ("6",     "BTN_9",  True),
             ("UP",    "BTN_14", True)],
            # row 2
            [("7",     "BTN_3",  True),
             ("8",     "BTN_4",  True),
             ("9",     "BTN_5",  True),
             ("DOWN",  "BTN_10", True)],
            # row 3
            [("#",     "BTN_1",  True),
             ("0",     "BTN_2",  True),
             ("",      None,     False),
             ("ENTER", "BTN_6",  True)],
        ]

        for r, row in enumerate(keys):
            for c, (label, btn_name, enabled) in enumerate(row):
                if not label:
                    tk.Label(parent, text="", width=8, bg="#f0f0f0").grid(
                        row=r, column=c, padx=3, pady=3, sticky="nsew"
                    )
                    continue

                if not enabled:
                    # greyed out — USB conflict
                    tk.Button(
                        parent,
                        text=f"{label}\n(USB)",
                        width=8, height=2,
                        state=tk.DISABLED,
                        bg="#bfbfbf", fg="#888888",
                        relief=tk.FLAT,
                        font=("Arial", 9)
                    ).grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
                    continue

                b = tk.Button(
                    parent,
                    text=label,
                    width=8, height=2,
                    bg="#d9d9d9", fg="black",
                    relief=tk.RAISED,
                    font=("Arial", 10, "bold"),
                    cursor="hand2"
                )
                b.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
                b.bind("<ButtonPress-1>",   lambda e, n=btn_name, l=label: self._key_press(n, l))
                b.bind("<ButtonRelease-1>", lambda e, n=btn_name, l=label: self._key_release(n, l))

        for c in range(4):
            parent.grid_columnconfigure(c, weight=1)

    # ── image / status helpers ──────────────────────────────────────────────

    def _placeholder(self):
        pw = self.current_width  * DISPLAY_SCALE
        ph = self.current_height * DISPLAY_SCALE
        self.canvas.config(width=pw, height=ph)
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, pw, ph, fill="#111")
        self.canvas.create_text(
            pw // 2, ph // 2,
            text="No Image\n\n1. Connect\n2. Start Stream\n3. Cause LCD updates",
            fill="#555",
            font=("Courier New", 10),
            justify=tk.CENTER
        )

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)

    def _toggle_connect(self):
        if self.worker and self.worker.running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            self._log("No port selected")
            return

        self.worker = SerialWorker(
            on_log=self._log,
            on_image=self._show_image,
            on_fw_status=self._update_fw_status
        )

        try:
            self.worker.connect(port)
            self.worker.set_decoder_mode(self.decoder_var.get())
            self.connect_btn.config(text="Disconnect")
            self.conn_status.config(text=f"Connected — {port}", fg="#007700")
        except Exception as e:
            self._log(f"Failed: {e}")

    def _disconnect(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.connect_btn.config(text="Connect")
        self.conn_status.config(text="Disconnected", fg="#666")

    def _decoder_changed(self, event=None):
        if self.worker:
            self.worker.set_decoder_mode(self.decoder_var.get())

    def _start_stream(self):
        if not self._check_connected():
            return
        self.worker.start_stream()

    def _stop_stream(self):
        if not self._check_connected():
            return
        self.worker.stop_stream()

    def _status(self):
        if not self._check_connected():
            return
        self.worker.send("STATUS")

    def _key_press(self, btn_name, label):
        if not self._check_connected():
            return
        self.worker.send(f"PRESS:{btn_name}")
        self._log(f"KEY DOWN: {label} ({btn_name})")

    def _key_release(self, btn_name, label):
        if not self._check_connected():
            return
        self.worker.send(f"RELEASE:{btn_name}")
        self._log(f"KEY UP: {label} ({btn_name})")

    def _save_png(self):
        if self.worker and self.worker.last_img:
            fname = f"capture_{int(time.time())}.png"
            self.worker.last_img.save(fname)
            self._log(f"Saved PNG: {fname}")
            messagebox.showinfo("Saved", f"Image saved as {fname}")
        else:
            self._log("No decoded image to save yet")

    def _save_raw(self):
        if self.worker and self.worker.last_raw:
            fname = f"capture_{int(time.time())}.bin"
            with open(fname, "wb") as f:
                f.write(self.worker.last_raw)
            self._log(f"Saved RAW: {fname}")
            messagebox.showinfo("Saved", f"Raw data saved as {fname}")
        else:
            self._log("No raw capture to save yet")

    def _check_connected(self):
        if not self.worker or not self.worker.running:
            self._log("Not connected!")
            return False
        return True

    def _show_image(self, img):
        def update():
            self.current_width, self.current_height = img.size
            scaled = img.resize(
                (self.current_width * DISPLAY_SCALE, self.current_height * DISPLAY_SCALE),
                Image.NEAREST
            )
            self._photo = ImageTk.PhotoImage(scaled)
            self.canvas.config(
                width=self.current_width  * DISPLAY_SCALE,
                height=self.current_height * DISPLAY_SCALE
            )
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.after(0, update)

    def _update_fw_status(self, status):
        def update():
            self.fw_status_label.config(text=f"Status: {status}")
        self.after(0, update)

    def _log(self, msg):
        def append():
            ts = time.strftime("%H:%M:%S")
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, f"[{ts}] {msg}\n")
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)
        self.after(0, append)

    def _clear_log(self):
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)

    def _on_close(self):
        if self.worker:
            self.worker.stop()
        self.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.resizable(True, True)
    app = KeypadWindow(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()