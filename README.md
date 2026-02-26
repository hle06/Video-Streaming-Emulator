# ESP32-Video-Streaming-Emulator
A high-performance screen-mirroring system that streams a live desktop feed to an ESP32 over WiFi. This project utilizes a custom binary protocol to handle real-time image processing and efficient data transmission.

## Technical Features

* Custom Transmission Protocol: Uses a specialized TCP-based protocol (v2) to synchronize frame IDs and manage pixel data.
* Dual-Mode Compression: Dynamically switches between per-pixel delta updates (PXUP) and Run-Length Encoding (PXUR) based on which method provides the smallest payload for the current frame.
* Optimized Hardware Performance: Leverages an 80MHz SPI clock and Direct Memory Access (DMA) on the ESP32 to ensure non-blocking display updates.
* Low-Latency Capture: Employs the mss library in Python for high-speed screen grabbing and OpenCV for real-time resizing and RGB565 color conversion.
* Adaptive Cursor Injection: Manually tracks and renders the mouse cursor onto the stream, ensuring visibility even when the OS capture prevents it.

## Hardware Requirements

* ESP32 Development Board: Recommended board with PSRAM support for large update buffers.
* Display: ST7789 IPS LCD with a 135x240 resolution.
* Connectivity: USB cable for power and a 2.4GHz WiFi connection.

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
| BLK | GPIO 4 | Backlight Enable |

## Installation and Setup

### 1. ESP32 Receiver
1. Open the receiver.ino file in the Arduino IDE.
2. Ensure the TFT_eSPI library is installed and configured for the ST7789 driver.
3. Enter your network credentials:
   const char* ssid = "YOUR_WIFI_SSID"; 
   const char* password = "YOUR_WIFI_PASSWORD";
4. Flash the code to your device and note the IP address displayed on the screen.

### 2. Python Transmitter
1. Install the required Python dependencies:
   pip install mss opencv-python numpy
2. Launch the stream using the ESP32 IP address:
   python transmitter.py --ip [DEVICE_IP] --show-cursor

## Protocol Specifications

The system monitors changes between frames and transmits data in one of two formats:
* PXUP (Pixel Updates): Sends individual pixel coordinates and colors.
* PXUR (Run-Length): Sends a starting point, a run length, and a single color to fill a horizontal line, significantly reducing bandwidth for solid UI elements.
