/*
 * ESP32 T-Display Pixel Receiver (ST7789 135x240)
 * Listens for per-pixel or run-length encoded updates over TCP.
 * Protocol v2 (little-endian):
 *   PXUP Header: 'P','X','U','P' (4B) + ver (1B, 0x02) + frame_id (uint32) + count (uint16)
 *   PXUP Body:   count * [x (uint8), y (uint8), color (uint16)]
 *   PXUR Header: 'P','X','U','R' (4B) + ver (1B, 0x01) + frame_id (uint32) + count (uint16)
 *   PXUR Body:   count * [y (uint8), x0 (uint8), len (uint8), color (uint16)]
 *
 * Performance optimizations:
 * - 80MHz SPI clock
 * - DMA transfers when available
 * - Batched display writes
 */

#include <WiFi.h>
#include <WiFiServer.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <esp_heap_caps.h>

#define TFT_MADCTL     0x36
#define TFT_MADCTL_RGB 0x00
#define TFT_MADCTL_BGR 0x08

// Screen parameters
#define SCREEN_W 135
#define SCREEN_H 240

TFT_eSPI display = TFT_eSPI();

// SPI frequency — reduce to 40000000 if display glitches occur
const uint32_t SPI_FREQ = 80000000;

// Wifi credentials
const char* ssid = "GTother"; 
const char* password = "GeorgeP@1927"; 

// TCP server
WiFiServer tcpServer(8090);
WiFiClient tcpClient;

// Protocol magic bytes and versions
const uint8_t HDR_MAGIC[4] = {'P', 'X', 'U', 'P'};
const uint8_t RUN_MAGIC[4] = {'P', 'X', 'U', 'R'};
const uint8_t HDR_VER      = 0x02;
const uint8_t RUN_VER      = 0x01;
const size_t  PKT_HDR_SIZE = 11;  // magic(4) + version(1) + frame_id(4) + count(2)

// Color mode flags
bool doSwapBytes = false;   // RGB565 arrives little-endian, no swap needed
bool isBgrPanel  = true;    // ST7789 panels typically use BGR ordering

// Statistics tracking
unsigned long totalFrames   = 0;
unsigned long statsTimer    = 0;
unsigned long pixelsWritten = 0;
uint32_t      curFrameId    = 0;

// Pixel data structure for buffering incoming updates
struct PixelData {
  uint8_t  x;
  uint8_t  y;
  uint8_t  runLen;
  uint16_t color;
};

PixelData* pixelBuf    = nullptr;
uint32_t   bufCapacity = 0;
bool       hasDma      = false;

// Allocate or grow the pixel buffer, preferring PSRAM
bool prepareBuffer(uint32_t needed) {
  if (needed <= bufCapacity && pixelBuf != nullptr) {
    return true;
  }
  PixelData* newBuf = (PixelData*)ps_malloc(needed * sizeof(PixelData));
  if (!newBuf) {
    newBuf = (PixelData*)malloc(needed * sizeof(PixelData));
  }
  if (!newBuf) {
    Serial.println("Buffer allocation failed");
    return false;
  }
  if (pixelBuf) {
    free(pixelBuf);
  }
  pixelBuf    = newBuf;
  bufCapacity = needed;
  return true;
}

// Read exactly 'len' bytes from the TCP stream
bool readFully(WiFiClient& conn, uint8_t* dest, size_t len) {
  size_t received = 0;
  while (received < len && conn.connected()) {
    int n = conn.read(dest + received, len - received);
    if (n > 0) {
      received += n;
    } else {
      delay(1);
    }
  }
  return received == len;
}

// Configure display color byte order
void setupColorMode() {
  display.setSwapBytes(doSwapBytes);
  display.writecommand(TFT_MADCTL);
  display.writedata(isBgrPanel ? TFT_MADCTL_BGR : TFT_MADCTL_RGB);
}

// Show idle screen with IP address while waiting for a connection
void renderIdleScreen() {
  display.fillScreen(TFT_BLACK);
  display.setTextColor(TFT_WHITE, TFT_BLACK);
  display.setCursor(10, 20);
  display.setTextSize(2);
  display.println("Pixel RX");
  display.setCursor(10, 50);
  display.setTextSize(1);
  display.println("IP Address:");
  display.setCursor(10, 70);
  display.setTextSize(2);
  display.setTextColor(TFT_GREEN, TFT_BLACK);
  display.println(WiFi.localIP().toString());
  display.setTextColor(TFT_WHITE, TFT_BLACK);
  display.setCursor(10, 100);
  display.setTextSize(1);
  display.println("Waiting for");
  display.setCursor(10, 115);
  display.println("connection...");
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ESP32 Pixel Receiver ===");

  // Enable backlight
  pinMode(4, OUTPUT);
  digitalWrite(4, HIGH);

  // Initialize display
  display.init();
  SPI.setFrequency(SPI_FREQ);
  hasDma = display.initDMA();
  display.setRotation(0);
  setupColorMode();
  display.fillScreen(TFT_BLACK);

  // Connect to WiFi
  Serial.print("Connecting to ");
  Serial.println(ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 30) {
    delay(250);
    Serial.print(".");
    retries++;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nFailed to connect to WiFi");
    display.fillScreen(TFT_RED);
    display.setTextColor(TFT_WHITE, TFT_RED);
    display.setCursor(10, 50);
    display.setTextSize(2);
    display.println("WiFi FAILED!");
    while (true) { delay(1000); }
  }

  Serial.println("\nConnected to WiFi");
  Serial.print("Device IP: ");
  Serial.println(WiFi.localIP());

  renderIdleScreen();

  tcpServer.begin();
  tcpServer.setNoDelay(true);
  Serial.println("Listening on port 8090");
}

// Process incoming data from the connected client
bool processConnection() {
  // Accept a new client if none is connected
  if (!tcpClient || !tcpClient.connected()) {
    tcpClient = tcpServer.available();
    if (tcpClient) {
      Serial.println("Client connected");
      tcpClient.setNoDelay(true);
      tcpClient.setTimeout(50);
      totalFrames   = 0;
      pixelsWritten = 0;
      display.fillScreen(TFT_BLACK);
    }
  }

  if (!tcpClient || !tcpClient.connected()) {
    return false;
  }

  // Need at least a full header before processing
  if (tcpClient.available() < 11) {
    return true;
  }

  // Read and identify the packet magic
  uint8_t magic[4];
  if (!readFully(tcpClient, magic, 4)) {
    tcpClient.stop();
    return false;
  }
  bool isRunPkt   = (memcmp(magic, RUN_MAGIC, 4) == 0);
  bool isPixelPkt = (memcmp(magic, HDR_MAGIC, 4) == 0);

  if (!isRunPkt && !isPixelPkt) {
    Serial.println("Invalid magic; dropping client");
    tcpClient.stop();
    return false;
  }

  // --- Handle PXUP (individual pixel updates) ---
  if (isPixelPkt) {
    uint8_t hdr[PKT_HDR_SIZE - 4];
    if (!readFully(tcpClient, hdr, sizeof(hdr))) {
      Serial.println("Incomplete pixel header; dropping");
      tcpClient.stop();
      return false;
    }
    if (hdr[0] != HDR_VER) {
      Serial.print("Unknown pixel version: 0x");
      Serial.println(hdr[0], HEX);
      tcpClient.stop();
      return false;
    }

    uint32_t frameId = (uint32_t)hdr[1] | ((uint32_t)hdr[2] << 8) |
                       ((uint32_t)hdr[3] << 16) | ((uint32_t)hdr[4] << 24);
    uint16_t count   = hdr[5] | (hdr[6] << 8);

    if (count == 0) {
      totalFrames++;
      curFrameId = frameId;
      return true;
    }
    if (count > (SCREEN_W * SCREEN_H)) {
      Serial.print("Pixel count exceeds screen: ");
      Serial.println(count);
      tcpClient.stop();
      return false;
    }

    if (!prepareBuffer(count)) {
      Serial.println("Cannot allocate pixel buffer; dropping");
      tcpClient.stop();
      return false;
    }

    // Read all pixel entries
    uint8_t raw[4];
    for (uint16_t i = 0; i < count; i++) {
      if (!readFully(tcpClient, raw, 4)) {
        Serial.println("Truncated pixel data; dropping");
        tcpClient.stop();
        return false;
      }
      pixelBuf[i].x     = raw[0];
      pixelBuf[i].y     = raw[1];
      pixelBuf[i].color = raw[2] | (raw[3] << 8);
    }

    // Push to display in a single transaction
    display.startWrite();
    for (uint16_t i = 0; i < count; i++) {
      uint8_t px = pixelBuf[i].x;
      uint8_t py = pixelBuf[i].y;
      if (px < SCREEN_W && py < SCREEN_H) {
        display.setAddrWindow(px, py, 1, 1);
        display.writeColor(pixelBuf[i].color, 1);
        pixelsWritten++;
      }
    }
    display.endWrite();

    totalFrames++;
    curFrameId = frameId;
    unsigned long now = millis();
    if (now - statsTimer > 2000) {
      Serial.print("Frames: ");
      Serial.print(totalFrames);
      Serial.print(" (frameId ");
      Serial.print(curFrameId);
      Serial.print(") | Pixels drawn: ");
      Serial.println(pixelsWritten);
      statsTimer = now;
    }
    return true;
  }

  // --- Handle PXUR (run-length encoded updates) ---
  uint8_t hdr[PKT_HDR_SIZE - 4];
  if (!readFully(tcpClient, hdr, sizeof(hdr))) {
    Serial.println("Incomplete run header; dropping");
    tcpClient.stop();
    return false;
  }
  if (hdr[0] != RUN_VER) {
    Serial.print("Unknown run version: 0x");
    Serial.println(hdr[0], HEX);
    tcpClient.stop();
    return false;
  }

  uint32_t frameId = (uint32_t)hdr[1] | ((uint32_t)hdr[2] << 8) |
                     ((uint32_t)hdr[3] << 16) | ((uint32_t)hdr[4] << 24);
  uint16_t count   = hdr[5] | (hdr[6] << 8);

  if (count == 0) {
    totalFrames++;
    curFrameId = frameId;
    return true;
  }
  if (count > (SCREEN_W * SCREEN_H)) {
    Serial.print("Run count exceeds limit: ");
    Serial.println(count);
    tcpClient.stop();
    return false;
  }

  if (!prepareBuffer(count)) {
    Serial.println("Cannot allocate run buffer; dropping");
    tcpClient.stop();
    return false;
  }

  // Read run entries: y(1) + x0(1) + length(1) + color(2) = 5 bytes each
  uint8_t raw[5];
  for (uint16_t i = 0; i < count; i++) {
    if (!readFully(tcpClient, raw, 5)) {
      Serial.println("Truncated run data; dropping");
      tcpClient.stop();
      return false;
    }
    pixelBuf[i].y      = raw[0];
    pixelBuf[i].x      = raw[1];
    pixelBuf[i].runLen  = raw[2];
    pixelBuf[i].color   = raw[3] | (raw[4] << 8);
  }

  // Render all runs in a single display transaction
  display.startWrite();
  for (uint16_t i = 0; i < count; i++) {
    uint8_t rx  = pixelBuf[i].x;
    uint8_t ry  = pixelBuf[i].y;
    uint8_t rln = pixelBuf[i].runLen;
    if (rx < SCREEN_W && ry < SCREEN_H && rln > 0 && (rx + rln) <= SCREEN_W) {
      display.setAddrWindow(rx, ry, rln, 1);
      if (hasDma) {
        display.pushBlock(pixelBuf[i].color, rln);
      } else {
        display.writeColor(pixelBuf[i].color, rln);
      }
      pixelsWritten += rln;
    }
  }
  display.endWrite();

  totalFrames++;
  curFrameId = frameId;
  unsigned long now = millis();
  if (now - statsTimer > 2000) {
    Serial.print("Frames: ");
    Serial.print(totalFrames);
    Serial.print(" (frameId ");
    Serial.print(curFrameId);
    Serial.print(") | Pixels drawn: ");
    Serial.println(pixelsWritten);
    statsTimer = now;
  }

  return true;
}

void loop() {
  processConnection();
  if (tcpClient && !tcpClient.connected()) {
    Serial.println("Client disconnected");
    renderIdleScreen();
  }
  delay(1);
}
