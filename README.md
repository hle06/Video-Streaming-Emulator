# ESP32-Video-Streaming-Emulator
A high-performance screen-mirroring system that streams a live desktop feed to an ESP32 over WiFi. This project utilizes a custom binary protocol to handle real-time image processing and efficient data transmission.

## Technical Features

* [cite_start]Custom Transmission Protocol: Uses a specialized TCP-based protocol (v2) to synchronize frame IDs and manage pixel data[cite: 2, 6, 7].
* [cite_start]Dual-Mode Compression: Dynamically switches between per-pixel delta updates (PXUP) and Run-Length Encoding (PXUR) based on which method provides the smallest payload for the current frame[cite: 8, 9].
* [cite_start]Optimized Hardware Performance: Leverages an 80MHz SPI clock and Direct Memory Access (DMA) on the ESP32 to ensure non-blocking display updates[cite: 4, 14, 68].
* Low-Latency Capture: Employs the mss library in Python for high-speed screen grabbing and OpenCV for real-time resizing and RGB565 color conversion.
* Adaptive Cursor Injection: Manually tracks and renders the mouse cursor onto the stream, ensuring visibility even when the OS capture prevents it.

## Hardware Requirements

* [cite_start]ESP32 Development Board: Recommended board with PSRAM support for large update buffers[cite: 14, 16].
* [cite_start]Display: ST7789 IPS LCD with a 135x240 resolution[cite: 1, 3].
* [cite_start]Connectivity: USB cable for power and a 2.4GHz WiFi connection[cite: 5, 27].

## Wiring Configuration

The system is configured for the following pinout by default:

| Component Pin | ESP32 Pin | Function |
| :--- | :--- | :--- |
| VCC | 3.3V | Power |
| GND | GND | Ground |
| SCL | GPIO 18 | SPI Clock |
| SDA | GPIO 23 | SPI MOSI |
| RES | GPIO 4 | Reset |
| DC | GPIO 2 | Data/Command |
| CS | GPIO 5 | Chip Select |
| BLK | GPIO 4 | [cite_start]Backlight Enable [cite: 25, 26] |

## Installation and Setup

### 1. ESP32 Receiver
1. Open the receiver.ino file in the Arduino IDE.
2. [cite_start]Ensure the TFT_eSPI library is installed and configured for the ST7789 driver[cite: 2].
3. Enter your network credentials:
   [cite_start]const char* ssid = "YOUR_WIFI_SSID"; [cite: 5]
   [cite_start]const char* password = "YOUR_WIFI_PASSWORD"; [cite: 5]
4. [cite_start]Flash the code to your device and note the IP address displayed on the screen[cite: 24].

### 2. Python Transmitter
1. Install the required Python dependencies:
   pip install mss opencv-python numpy
2. Launch the stream using the ESP32 IP address:
   python transmitter.py --ip [DEVICE_IP] --show-cursor

## Protocol Specifications

The system monitors changes between frames and transmits data in one of two formats:
* [cite_start]PXUP (Pixel Updates): Sends individual pixel coordinates and colors[cite: 6, 7].
* [cite_start]PXUR (Run-Length): Sends a starting point, a run length, and a single color to fill a horizontal line, significantly reducing bandwidth for solid UI elements[cite: 8, 9].
