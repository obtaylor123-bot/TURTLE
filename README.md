🔐 Lock Testing System (STM32F1)
Overview

This project is a manufacturing and diagnostic GUI tool used to detect, identify, and test STM32F1-based lock and entry control boards.

The application provides a single unified interface for:

Device discovery over serial (COM ports)

Firmware identification via Black Magic Probe (BMP)

Automated lock and entry board testing

Detailed logging for manufacturing and QA validation

⚠️ Note: The source code for this project is private and not publicly accessible.

Features

🔍 Automatic COM Port Detection

🧠 STM32F1 Device Identification

🧪 Automated Lock I/O Testing

🖥️ Entry Board Display Verification

🔌 Black Magic Probe Firmware Detection

🧾 Detailed, Timestamped Test Logs

💾 Save / Load Device State

🧵 Threaded Operations (Non-Blocking GUI)

Intended Use

This tool is designed for:

Manufacturing test stations

Hardware bring-up and validation

QA testing of embedded lock systems

Engineering diagnostics during development

It is not intended for end users.

User Interface

The GUI consists of:

Left Panel: Device discovery and test controls

Center Panel: High-level test status (PASS / FAIL)

Right Panel: Detailed diagnostic log output

All hardware communication occurs over serial interfaces.

Hardware Requirements

STM32F1-based lock or entry control boards

Black Magic Probe (BMP) debugger

USB serial connection

Windows test station (COM ports)

Software Requirements

Python 3.x

Tkinter

pySerial

(Additional internal modules are used but not publicly documented.)

Repository Visibility

🔒 This repository is private

Due to proprietary hardware interfaces and internal testing logic, the source code is not publicly visible.
This README exists to document functionality and purpose only.

Status

✔️ Actively used in manufacturing/testing
✔️ Stable and production-ready
🚧 Ongoing internal improvements

Contact

For access, questions, or collaboration, please contact the repository owner directly.
