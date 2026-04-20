from logging import root
import os
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
import json
import serial
import serial.tools.list_ports
import time
import threading
import subprocess
import manufacturing
import asyncio 
import testFlash
import keypad 
from bleak import BleakScanner
from tkinter import filedialog as fd
# Import the STM32F1 identification module and test modules
#from identification_STM32F1 import get_bmp_version 
from test_identification_entry2 import identify_entries_display
from test_identification_locks2 import identify_locks
from MCP2221_functions import HW_reboot, HW_init

# Global variables
available_COM_ports = []
STM32F1_com_ports = []
unit_information = []  # Format: [(port, device_type, mcu_id, lock_position)]
FIRMWARE_PATH = "Not Selected"
STATE_FILE = "device_state.json"

def select_firmware():
    global FIRMWARE_PATH
    
    # Open file dialog to select the firmware file
    file_path = fd.askopenfilename(
        title="Select Firmware File",
        initialdir=os.getcwd(), # Starts in your current project folder
        filetypes=[
            ("Firmware Files", " *.hex"),
            ("Binary Files", "*.bin"),
            ("Intel Hex Files", "*.hex"),
            ("All files", "*.*")
        ]
    )
    
    if file_path:
        FIRMWARE_PATH = file_path
        # Update the UI to show the user what is selected
        append_to_right_text(f"[System] 📂 Firmware Selected: {os.path.basename(file_path)}")
        
        # Optional: Clear middle text and show the full path for verification
        middle_text.config(state=tk.NORMAL)
        append_to_middle_text(f"\n🚀 TARGET FIRMWARE SET TO:\n{FIRMWARE_PATH}\n")
        middle_text.config(state=tk.DISABLED)
    else:
        append_to_right_text("[System] ⚠️ Firmware selection cancelled.")

def save_state():
    global available_COM_ports, STM32F1_com_ports, unit_information
    
    # 1. Gather all "Now" data into the dictionary
    data_to_save = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "available_COM_ports": available_COM_ports,
        "STM32F1_com_ports": STM32F1_com_ports,
        "unit_information": unit_information,
        "log_summary": middle_text.get(1.0, tk.END).strip()
    }
    
    # 2. Open the File Dialog to pick a save location
    file_path = fd.asksaveasfilename(
        initialfile=f"Test_Result_{time.strftime('%H%M%S')}.json",
        defaultextension=".json",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
    )
    
    if file_path:
        try:
            with open(file_path, 'w', encoding="utf-8") as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=4)
            append_to_right_text(f"[Save] ✅ Successfully saved to: {os.path.basename(file_path)}")
        except Exception as e:
            append_to_right_text(f"[Save] ❌ Save Failed: {e}")

def load_state():
    global available_COM_ports, STM32F1_com_ports, unit_information
    
    # 1. Open the File Dialog to pick a file to load
    file_path = fd.askopenfilename(
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
    )
    
    if file_path:
        try:
            with open(file_path, 'r', encoding="utf-8") as f:
                data = json.load(f)
            
            # 2. Restore all global lists
            available_COM_ports = data.get("available_COM_ports", [])
            STM32F1_com_ports = data.get("STM32F1_com_ports", [])
            unit_information = data.get("unit_information", [])
            
            # 3. Restore the middle console text
            saved_log = data.get("log_summary", "")
            middle_text.config(state=tk.NORMAL)
            middle_text.delete(1.0, tk.END)
            middle_text.insert(tk.END, f"--- LOADED FROM {data.get('timestamp')} ---\n")
            middle_text.insert(tk.END, saved_log)
            middle_text.config(state=tk.DISABLED)
            
            append_to_right_text(f"[Load] ✅ Restored state from {os.path.basename(file_path)}")
            
        except Exception as e:
            append_to_right_text(f"[Load] ❌ Load Failed: {e}")

def append_to_right_text(text):
    right_text.config(state=tk.NORMAL) # Unlock for writing
    right_text.insert(tk.END, str(text) + "\n")
    right_text.see(tk.END)
    right_text.config(state=tk.DISABLED) # Lock again

def append_to_middle_text(text):
    middle_text.config(state=tk.NORMAL) # Unlock for writing
    middle_text.insert(tk.END, str(text) + "\n")
    middle_text.see(tk.END)
    middle_text.config(state=tk.DISABLED) # Lock again

def find_all_ports():
    middle_text.config(state=tk.NORMAL)
    middle_text.delete(1.0, tk.END)
    middle_text.insert(tk.END, "Finding all COM ports\n")
    middle_text.config(state=tk.DISABLED)

    append_to_right_text("\n[Find all ports] Starting...\n")

    def task():
        global available_COM_ports
        available_COM_ports = [port.device for port in serial.tools.list_ports.comports()]

        output = f"[Find all ports] Found {len(available_COM_ports)} COM ports\n"
        output += f"Available ports: {available_COM_ports}\n"
    
        right_text.after(0, lambda: append_to_right_text(output))
        middle_text.after(0, lambda: append_to_middle_text(f"Found {len(available_COM_ports)} ports\n"))

    threading.Thread(target=task, daemon=True).start()
    
def find_all_STM32F1():
    """Starts the Black Pill detection in a background thread."""
    append_to_right_text("🔍 Scanning SWD Chain...")
    # Change button state to disabled so user doesn't spam it
    # btn_find_stm.config(state=tk.DISABLED) 
    
    thread = threading.Thread(target=verify_swd_connection, daemon=True)
    thread.start()

def verify_swd_connection():
    """Targets COM14 and COM16 specifically to find Black Pills."""
    global STM32F1_com_ports
    found_any_target = False
    
    # Targeting the GDB server ports you verified
    target_ports = ["COM14", "COM16"]
    
    append_to_right_text("🚀 Starting direct scan on COM14 & COM16...")

    for port in target_ports:
        # UNC prefix is required for COM ports > 9 in GDB
        gdb_port = f"\\\\.\\{port}"
        
        try:
            # We run version first. If this works, the PORT is correct.
            gdb_cmd = [
                "arm-none-eabi-gdb", "--batch", "--quiet", "-nx",
                "-ex", f"target extended-remote {gdb_port}",
                "-ex", "monitor version",
                "-ex", "monitor swdp_scan",
                "-ex", "quit"
            ]
            
            result = subprocess.run(gdb_cmd, capture_output=True, text=True, timeout=5)
            output = (result.stdout + result.stderr).upper()

            # CHECK 1: Did we get the 'Black Magic Probe' string from your terminal?
            if "BLACK MAGIC PROBE" in output:
                # Find that specific version line from your screenshot
                version_info = "BMP Detected"
                for line in output.splitlines():
                    if "BLACK MAGIC PROBE" in line:
                        version_info = line.strip()
                        break
                
                append_to_middle_text(f"\n[Probe Found] {version_info} on {port}")

                # CHECK 2: Is the silicon chip (the pill) responding to the scan?
                if any(x in output for x in ["STM32F4", "STM32F1", "0X1BA01477", "ATT DRIVER"]):
                    found_any_target = True
                    append_to_right_text(f"✅ CONNECTED: Target detected on {port}")
                    if port not in STM32F1_com_ports:
                        STM32F1_com_ports.append(port)
                else:
                    # This matches your 'SWD scan failed' terminal error
                    append_to_right_text(f"❌ EMPTY: {port} is active, but check wires/power.")
                    append_to_middle_text(f"   ⚠️ 'SWD scan failed' - Probe is alive, chip is not.\n")
            
            else:
                # If we get here, the port is likely locked by another app or GDB
                append_to_right_text(f"⚠️ {port}: No response (Check if terminal is open!)")

        except subprocess.TimeoutExpired:
            append_to_right_text(f"🕒 TIMEOUT: {port} took too long.")
        except Exception as e:
            append_to_right_text(f"❗ Error: {str(e)}")

    if not found_any_target:
        append_to_right_text("⚠️ No active Black Pills detected.")
        
def flash_firmware():
    global FIRMWARE_PATH
    
    # 1. Validation Check
    if FIRMWARE_PATH == "Not Selected" or not os.path.exists(FIRMWARE_PATH):
        append_to_right_text("❌ Error: No firmware file selected!")
        middle_text.config(state=tk.NORMAL)
        append_to_middle_text("\n⚠️ PLEASE SELECT A .BIN OR .HEX FILE FIRST\n")
        middle_text.config(state=tk.DISABLED)
        return

    # 2. Identify the BMP Port (Assuming COM4/COM5 based on your screenshots)
    # You can also use a global variable for this if it changes
    bmp_port = "COM5" 

    append_to_right_text(f"⚡ Flashing: {os.path.basename(FIRMWARE_PATH)}...")
    append_to_middle_text(f"\n--- Starting Flash Sequence ---\nTarget: {FIRMWARE_PATH}\n")

    def run_flash_task():
        try:
            # Construct the GDB command for Black Magic Probe
            # This tells GDB to: 
            # - Connect to the BMP
            # - Scan for the STM32
            # - Attach to the first target
            # - Load the file you selected
            # - Kill the session and exit
            gdb_commands = [
                "arm-none-eabi-gdb",
                "-nx", 
                "--batch",
                "-ex", f"target extended-remote {bmp_port}",
                "-ex", "monitor swdp_scan",
                "-ex", "attach 1",
                "-ex", f"load {FIRMWARE_PATH}",
                "-ex", "compare-sections",
                "-ex", "kill",
                "-ex", "quit"
            ]

            # Execute the command and capture the output
            process = subprocess.Popen(
                gdb_commands, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                text=True
            )
            stdout, stderr = process.communicate()

            # 3. Handle Results
            if "Transfer rate" in stdout:
                append_to_right_text("✅ Flash Successful!")
                append_to_middle_text("\n✨ FIRMWARE LOADED SUCCESSFULLY\n")
            else:
                append_to_right_text("❌ Flash Failed.")
                append_to_middle_text(f"\n⚠️ ERROR DURING FLASH:\n{stderr}\n{stdout}\n")

        except Exception as e:
            append_to_right_text(f"⚠️ System Error: {str(e)}")

    # Run in a thread so the GUI doesn't freeze
    threading.Thread(target=run_flash_task, daemon=True).start()

def find_all_locks():
    middle_text.config(state=tk.NORMAL)
    middle_text.delete(1.0, tk.END)
    middle_text.config(state=tk.DISABLED)
    append_to_middle_text("🚀 Starting STM32 Manufacturing Cycle...\n")
    
    def task():
        global unit_information
        lock_units = [info for info in unit_information if "STM32" in info[1]]
        
        if not lock_units:
            append_to_middle_text("⚠️ No STM32 Lock Targets found.")
            return

        for port, target_type, status in lock_units:
            append_to_right_text(f"\n[Lock @ {port}] Flashing Firmware...")
            if flash_firmware(port, "firmware/lock_actuator_mfg.elf"):
                append_to_right_text(f"✅ Flash Success.")
                uart_port = "COM5" 
                manufacturing.init_serial_link(uart_port, 115200)
                append_to_right_text(f"⚙️ Entering Mfg Mode & Rebooting...")
                append_to_right_text(f"🔑 Running 5-Step Cert Process...")
                success = manufacturing.the_five_step_process(
                    fuse=True, 
                    delay=0.5, 
                    customer_keys=manufacturing.DEFAULT_USE_CUSTOMER_KEYS
                )
                
                if success:
                    append_to_middle_text(f"✅ Lock {port}: MFG COMPLETE")
                else:
                    append_to_middle_text(f"❌ Lock {port}: 5-Step Process Failed")
                
                manufacturing.deinit_serial_link()
            else:
                append_to_right_text(f"❌ Flash Failed on {port}")

    threading.Thread(target=task, daemon=True).start()

def find_all_entries():
    middle_text.config(state=tk.NORMAL)
    middle_text.delete(1.0, tk.END)
    middle_text.config(state=tk.DISABLED)
    append_to_middle_text("🚀 Starting nRF Manufacturing Cycle...\n")
    
    def task():
        entry_units = [info for info in unit_information if "nRF" in info[1]]
        if not entry_units:
            append_to_middle_text("⚠️ No nRF Entry Targets found.")
            return

        for port, target_type, status in entry_units:
            append_to_right_text(f"\n[Entry @ {port}] Flashing Firmware...")
            if flash_firmware(port, "firmware/lock_face_mfg.elf"):
                append_to_right_text(f"✅ Flash Success.")
                uart_port = "COM7" 
                manufacturing.init_serial_link(uart_port, 115200)
                append_to_right_text(f"🔑 Running 5-Step Cert Process...")
                success = manufacturing.the_five_step_process(fuse=True)
                
                if success:
                    append_to_middle_text(f"✅ Entry {port}: MFG COMPLETE")
                else:
                    append_to_middle_text(f"❌ Entry {port}: 5-Step Process Failed")
                
                manufacturing.deinit_serial_link()
            else:
                append_to_right_text(f"❌ Flash Failed on {port}")

    threading.Thread(target=task, daemon=True).start()

def find_beacon():
    middle_text.config(state=tk.NORMAL)
    middle_text.delete(1.0, tk.END)
    middle_text.config(state=tk.DISABLED)
    append_to_middle_text("📡 Scanning for Bluetooth Beacon...")
    
    def task():
        try:
            import asyncio
            from bleak import BleakScanner
            TARGET_NAME_PREFIX = "YGR" 
            found_device = None

            async def run_scan():
                nonlocal found_device
                devices = await BleakScanner.discover(timeout=5.0)
                for d in devices:
                    if d.name and TARGET_NAME_PREFIX in d.name.upper():
                        found_device = d
                        break

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_scan())

            if found_device:
                append_to_middle_text(f"✅ Found: {found_device.name}")
                append_to_right_text(f"✅ BLE: {found_device.name} ({found_device.address}) detected.")
            else:
                append_to_middle_text("❌ Beacon not detected")
                append_to_right_text("➖ BLE: Scan completed, target not found.")

        except ImportError:
            append_to_right_text("❌ ERROR: 'bleak' library not installed. Run 'pip install bleak'")
        except Exception as e:
            append_to_right_text(f"❌ BLE Error: {str(e)}")

    threading.Thread(target=task, daemon=True).start()

def run_full_test_sequence():
    """
    Step-by-Step: 
    1. Flash 
    2. Enable Mfg Mode 
    3. Hardware Reboot 
    4. Five Step Process
    """
    middle_text.config(state=tk.NORMAL)
    middle_text.delete(1.0, tk.END)
    middle_text.config(state=tk.DISABLED)
    append_to_middle_text("🚀 Starting Full Manufacturing Sequence...")

    def task():
        global unit_information
        if not unit_information:
            append_to_middle_text("⚠️ No units found. Please scan first.")
            return

        for port, device_type, status in unit_information:
            append_to_right_text(f"\n[Target @ {port}] Initializing...")

            # 1. FLASHING - change to FIRMWARE path, also its HEX now 
            firmware = "lock_actuator_mfg.elf" if "STM32" in device_type else "lock_face_mfg.elf"
            append_to_right_text(f"💾 Flashing {firmware}...")
            
            if not flash_firmware(port, f"firmware/{firmware}"):
                append_to_middle_text(f"❌ {port}: Flash Failed")
                continue

            # 2. ENABLE MANUFACTURING MODE
            # We assume the UART port is 1 index higher than the GDB port - change to the com of the UART drive
            uart_port = f"COM{int(port[3:]) + 1}" 
            append_to_right_text(f"⚙️ Enabling Mfg Mode on {uart_port}...")
            
            try:
                # Manufacturing.py already init may not need to reinit 
                manufacturing.init_serial_link(uart_port, 115200)
                # This sends the specific command to the Black Pill to set the flag
                manufacturing.enter_manufacturing_mode() 
                time.sleep(0.5)
                
                # 3. REBOOT THE DEVICE
                append_to_right_text("🔄 Triggering Hardware Reboot via MCP2221...")
                HW_reboot() # Power cycle via your MCP2221 functions -  might not need
                time.sleep(2.0) # Wait for device to initialize after reboot
                
                # Re-establish link if the reboot dropped the serial connection
                manufacturing.init_serial_link(uart_port, 115200)

                # 4. FIVE STEP PROCESS
                append_to_right_text("🔑 Running 5-Step Certification...")
                success = manufacturing.the_five_step_process(fuse=True)
                
                if success:
                    append_to_middle_text(f"✅ {port}: PROVISIONED SUCCESSFULLY")
                    append_to_right_text(f"✅ {port}: Complete.")
                else:
                    append_to_middle_text(f"❌ {port}: 5-Step Process Failed")
                
                manufacturing.deinit_serial_link()

            except Exception as e:
                append_to_right_text(f"❌ Error during sequence: {str(e)}")
                manufacturing.deinit_serial_link()

    threading.Thread(target=task, daemon=True).start()

# GUI logic (unchanged except for initial widget state)
right_visible = True
def toggle_detailed_log():
    global right_visible
    if right_visible:
        right_frame.grid_remove()
        toggle_log_btn.config(text="Show Detailed Log")
    else:
        right_frame.grid()
        toggle_log_btn.config(text="Hide Detailed Log")
    right_visible = not right_visible

keypad_win = None

def open_lock_pad():
    """Opens the Keypad and SPI viewer window."""
    # Check if a window is already open to avoid multiples (optional)
    #root = tk.Tk()
    global keypad_win
    
    if keypad_win is not None and tk.Toplevel.winfo_exists(keypad_win):
        keypad_win.lift()  
    else:
        keypad_win = keypad.KeypadWindow(root)

    #keypad_win = keypad.KeypadWindow(root) 



root = tk.Tk()
root.title("Lock Testing System (STM32F1 Only)")
root.state('zoomed')

root.grid_columnconfigure(0, weight=0)
root.grid_columnconfigure(1, weight=1)
root.grid_columnconfigure(2, weight=2)
root.grid_rowconfigure(0, weight=1)

left_frame = tk.Frame(root, bg="#f0f0f0")
left_frame.grid(row=0, column=0, sticky="ns", padx=10, pady=10)

title_label = tk.Label(left_frame, text="Lock Test System", font=("Arial", 14, "bold"), bg="#f0f0f0")
title_label.pack(pady=(10, 20))

tk.Label(left_frame, text="1. Setup", font=("Arial", 10, "bold"), bg="#f0f0f0", fg="#333").pack(pady=(5, 2))
tk.Button(left_frame, text="Find COM Ports", command=find_all_ports, width=20, height=2, bg="#2196F3", fg="white", font=("Arial", 9)).pack(pady=3)
tk.Button(left_frame, text="Find STM32F1", command=find_all_STM32F1, width=20, height=2, bg="#4CAF50", fg="white", font=("Arial", 9, "bold")).pack(pady=3)
btn_select_fw = tk.Button(
    left_frame, # Change this to the frame where you want the button
    text="📂 Select Firmware",
    command=select_firmware,
    width=20,
    height=2,
    bg="#34495e", # Dark professional blue
    fg="white",
    font=("Arial", 9, "bold")
)
btn_select_fw.pack(pady=5)

tk.Frame(left_frame, height=2, bg="#ccc").pack(fill=tk.X, pady=10)
tk.Label(left_frame, text="2. Device Tests", font=("Arial", 10, "bold"), bg="#f0f0f0", fg="#333").pack(pady=(5, 2))
tk.Button(left_frame, text="Test Locks", command=find_all_locks, width=20, height=2, bg="#FF9800", fg="white", font=("Arial", 9)).pack(pady=3)
tk.Button(left_frame, text="Test Entries", command=find_all_entries, width=20, height=2, bg="#FF9800", fg="white", font=("Arial", 9)).pack(pady=3)
tk.Button(left_frame, text="Open Lock Pad", command=open_lock_pad, width=20, bg="#673AB7", fg="white").pack(pady=5)

tk.Frame(left_frame, height=2, bg="#ccc").pack(fill=tk.X, pady=10)
tk.Label(left_frame, text="3. Complete Test", font=("Arial", 10, "bold"), bg="#f0f0f0", fg="#333").pack(pady=(5, 2))
tk.Button(left_frame, text="▶ Run Full Test", command=run_full_test_sequence, width=20, height=2, bg="#E91E63", fg="white", font=("Arial", 9, "bold")).pack(pady=3)

tk.Frame(left_frame, height=2, bg="#ccc").pack(fill=tk.X, pady=10)
tk.Label(left_frame, text="4. Settings", font=("Arial", 10, "bold"), bg="#f0f0f0", fg="#333").pack(pady=(5, 2))
tk.Button(left_frame, text="Save State", command=save_state, width=20, bg="#9E9E9E", fg="white", font=("Arial", 9)).pack(pady=3)
tk.Button(left_frame, text="Load State", command=load_state, width=20, bg="#9E9E9E", fg="white", font=("Arial", 9)).pack(pady=3)

toggle_log_btn = tk.Button(left_frame, text="Hide Detailed Log", command=toggle_detailed_log, width=20, bg="#607D8B", fg="white")

tk.Label(left_frame, text="5. Wireless Scan", font=("Arial", 10, "bold"), bg="#f0f0f0", fg="#333").pack(pady=(5, 2))
tk.Button(left_frame, text="📡 Detect Beacon", command=find_beacon, width=20, height=2, bg="#00BCD4", fg="white", font=("Arial", 9, "bold")).pack(pady=3)
toggle_log_btn.pack(pady=3)

middle_frame = tk.Frame(root)
middle_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
tk.Label(middle_frame, text="Test Status", font=("Arial", 12, "bold")).pack(anchor="w")
# Start with state=DISABLED
middle_text = ScrolledText(middle_frame, wrap="word", font=("Consolas", 10), state=tk.DISABLED)
middle_text.pack(fill=tk.BOTH, expand=True)

right_frame = tk.Frame(root)
right_frame.grid(row=0, column=2, sticky="nsew", padx=10, pady=10)
tk.Label(right_frame, text="Detailed Log", font=("Arial", 12, "bold")).pack(anchor="w")
# Start with state=DISABLED
right_text = ScrolledText(right_frame, wrap="word", font=("Consolas", 9), bg="#1e1e1e", fg="#00ff00", state=tk.DISABLED)
right_text.pack(fill=tk.BOTH, expand=True)

# Use append helpers for initial messages
append_to_right_text("=" * 60)
append_to_right_text("Lock Testing System - STM32F1 Devices Only")
append_to_right_text("=" * 60)
append_to_right_text("\nWorkflow:")
append_to_right_text("1. Find COM Ports")
append_to_right_text("2. Find STM32F1 boards")
append_to_right_text("\nDevice Testing:")
append_to_right_text("   - Test Locks (identify_locks)")
append_to_right_text("   - Test Entries (identify_entries_display)")
append_to_right_text("3. Or run full test sequence\n")

append_to_middle_text("Lock Testing System\n")
append_to_middle_text("STM32F1 Devices Only\n\n")
append_to_middle_text("Ready to test:\n")
append_to_middle_text("• Locks\n")
append_to_middle_text("• Entry Boards\n\n")
append_to_middle_text("Start by finding devices\n")

root.mainloop()