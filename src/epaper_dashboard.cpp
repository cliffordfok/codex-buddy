#include "epaper_dashboard.h"

#if defined(CONFIG_IDF_TARGET_ESP32S3)

#include <GxEPD2_BW.h>
#include <M5Unified.h>
#include <SPI.h>
#include <ctype.h>
#include <string.h>

#include "buddy.h"

namespace {

// Waveshare 4.2-inch e-Paper Module Rev2.2 (400x300, black/white).
constexpr int EPD_BUSY = 4;
constexpr int EPD_RST  = 7;
constexpr int EPD_DC   = 1;
constexpr int EPD_CS   = 8;
constexpr int EPD_CLK  = 5;
constexpr int EPD_DIN  = 6;

constexpr uint32_t MIN_REFRESH_MS = 30000;
constexpr uint8_t PARTIALS_BEFORE_FULL = 12;
constexpr int PET_W = 135;
constexpr int PET_H = 96;

SPIClass epaperSpi(FSPI);
GxEPD2_BW<GxEPD2_420_GDEY042T81, GxEPD2_420_GDEY042T81::HEIGHT> epaper(
    GxEPD2_420_GDEY042T81(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));
M5Canvas epaperPet(&M5.Lcd);

bool ready = false;
bool rendered = false;
uint8_t partialRefreshes = 0;
uint32_t lastRefreshMs = 0;
uint32_t lastSignature = 0;

void serviceWhileBusy(const void*) {
  // Keep the ESP32-S3 scheduler and BLE host tasks moving during the panel's
  // multi-second waveform sequence.
  delay(1);
}

uint32_t hashByte(uint32_t hash, uint8_t value) {
  return (hash ^ value) * 16777619UL;
}

uint32_t hashU32(uint32_t hash, uint32_t value) {
  for (uint8_t i = 0; i < 4; ++i) {
    hash = hashByte(hash, (uint8_t)(value & 0xFF));
    value >>= 8;
  }
  return hash;
}

uint32_t hashText(uint32_t hash, const char* text) {
  if (!text) return hashByte(hash, 0);
  while (*text) hash = hashByte(hash, (uint8_t)*text++);
  return hashByte(hash, 0);
}

uint32_t minutesLeft(uint32_t resetAt, uint32_t utcNow) {
  if (resetAt == 0 || utcNow == 0) return UINT32_MAX;
  if (resetAt <= utcNow) return 0;
  return (resetAt - utcNow + 59) / 60;
}

uint32_t signatureFor(const EpaperDashboardState& s) {
  uint32_t hash = 2166136261UL;
  hash = hashByte(hash, s.live ? 1 : 0);
  hash = hashByte(hash, s.usageAvailable ? 1 : 0);
  hash = hashByte(hash, s.batteryAvailable ? 1 : 0);
  hash = hashByte(hash, s.batteryPct);
  hash = hashByte(hash, s.primaryUsed);
  hash = hashByte(hash, s.secondaryUsed);
  hash = hashByte(hash, s.personaState);
  hash = hashByte(hash, buddySpeciesIdx());
  hash = hashU32(hash, minutesLeft(s.primaryResetsAt, s.utcNow));
  hash = hashU32(hash, minutesLeft(s.secondaryResetsAt, s.utcNow));
  hash = hashU32(hash, s.tokens);
  return hashText(hash, s.stateLabel);
}

void printUpper(const char* text) {
  if (!text) return;
  while (*text) {
    epaper.write((uint8_t)toupper((unsigned char)*text));
    ++text;
  }
}

void formatTokens(uint32_t value, char* out, size_t len) {
  if (value >= 1000000) {
    snprintf(out, len, "%lu.%luM", (unsigned long)(value / 1000000),
             (unsigned long)((value / 100000) % 10));
  } else if (value >= 1000) {
    snprintf(out, len, "%luK", (unsigned long)(value / 1000));
  } else {
    snprintf(out, len, "%lu", (unsigned long)value);
  }
}

void formatReset(uint32_t resetAt, uint32_t utcNow, char* out, size_t len) {
  uint32_t mins = minutesLeft(resetAt, utcNow);
  if (mins == UINT32_MAX) {
    snprintf(out, len, "RESET --");
  } else if (mins == 0) {
    snprintf(out, len, "RESET SOON");
  } else if (mins >= 1440) {
    snprintf(out, len, "RESET %lud %02luh", (unsigned long)(mins / 1440),
             (unsigned long)((mins / 60) % 24));
  } else if (mins >= 60) {
    snprintf(out, len, "RESET %luh %02lum", (unsigned long)(mins / 60),
             (unsigned long)(mins % 60));
  } else {
    snprintf(out, len, "RESET %lum", (unsigned long)mins);
  }
}

void drawPet(uint8_t personaState) {
  const int x0 = 5;
  const int y0 = 66;
  bool canvasReady = epaperPet.width() == PET_W && epaperPet.height() == PET_H;
  if (!canvasReady) {
    epaper.drawCircle(72, 112, 34, GxEPD_BLACK);
    epaper.fillCircle(60, 103, 3, GxEPD_BLACK);
    epaper.fillCircle(84, 103, 3, GxEPD_BLACK);
    epaper.drawLine(61, 124, 70, 130, GxEPD_BLACK);
    epaper.drawLine(70, 130, 84, 122, GxEPD_BLACK);
    return;
  }

  epaperPet.fillSprite(0x0000);
  buddyRenderTo(&epaperPet, personaState);
  for (int y = 0; y < PET_H; ++y) {
    for (int x = 0; x < PET_W; ++x) {
      if (epaperPet.readPixel(x, y) != 0) {
        epaper.drawPixel(x0 + x, y0 + y, GxEPD_BLACK);
      }
    }
  }
}

void drawUsageMeter(int y, const char* label, uint8_t usedPct,
                    uint32_t resetAt, const EpaperDashboardState& s) {
  constexpr int x = 158;
  constexpr int w = 232;
  bool available = s.live && s.usageAvailable && resetAt != 0;
  uint8_t remaining = usedPct >= 100 ? 0 : (uint8_t)(100 - usedPct);

  epaper.setTextColor(GxEPD_BLACK);
  epaper.setTextSize(2);
  epaper.setCursor(x, y);
  epaper.print(label);

  char pct[12];
  if (available) snprintf(pct, sizeof(pct), "%u%% LEFT", remaining);
  else snprintf(pct, sizeof(pct), "-- LEFT");
  int16_t bx, by;
  uint16_t tw, th;
  epaper.getTextBounds(pct, 0, 0, &bx, &by, &tw, &th);
  epaper.setCursor(390 - tw, y);
  epaper.print(pct);

  int barY = y + 24;
  epaper.drawRect(x, barY, w, 20, GxEPD_BLACK);
  if (available && remaining > 0) {
    int fill = (int)((uint32_t)(w - 4) * remaining / 100);
    if (fill > 0) epaper.fillRect(x + 2, barY + 2, fill, 16, GxEPD_BLACK);
  }

  char reset[24];
  formatReset(available ? resetAt : 0, available ? s.utcNow : 0,
              reset, sizeof(reset));
  epaper.setTextSize(1);
  epaper.setCursor(x, barY + 28);
  epaper.print(reset);
}

void drawBattery(const EpaperDashboardState& s) {
  constexpr int x = 272;
  constexpr int y = 12;
  constexpr int innerW = 16;

  epaper.drawRect(x, y, 20, 10, GxEPD_BLACK);
  epaper.fillRect(x + 20, y + 3, 3, 4, GxEPD_BLACK);
  if (s.batteryAvailable && s.batteryPct > 0) {
    int fillW = (int)((uint32_t)innerW * s.batteryPct / 100);
    if (fillW > 0) epaper.fillRect(x + 2, y + 2, fillW, 6, GxEPD_BLACK);
  }

  epaper.setTextSize(1);
  epaper.setCursor(300, 16);
  if (s.batteryAvailable) {
    epaper.printf("%u%%", s.batteryPct);
  } else {
    epaper.print("--%");
  }
}

void drawDashboard(const EpaperDashboardState& s) {
  epaper.fillScreen(GxEPD_WHITE);
  epaper.setTextColor(GxEPD_BLACK);
  epaper.setTextWrap(false);

  epaper.setTextSize(2);
  epaper.setCursor(8, 10);
  epaper.print("CODEX USAGE");
  epaper.setTextSize(1);
  drawBattery(s);
  epaper.setCursor(344, 16);
  epaper.print(s.live ? "[ LIVE ]" : "[ WAIT ]");
  epaper.drawFastHLine(8, 38, 384, GxEPD_BLACK);
  epaper.drawFastVLine(146, 48, 210, GxEPD_BLACK);

  epaper.setTextSize(1);
  epaper.setCursor(8, 51);
  epaper.print("PET SNAPSHOT");
  drawPet(s.personaState);

  epaper.setCursor(8, 172);
  epaper.print("STATE  ");
  printUpper(s.stateLabel ? s.stateLabel : "idle");
  epaper.setCursor(8, 188);
  epaper.print("PET    ");
  printUpper(buddySpeciesName());
  char tokens[16];
  formatTokens(s.tokens, tokens, sizeof(tokens));
  epaper.setCursor(8, 204);
  epaper.print("TOKENS ");
  epaper.print(tokens);

  drawUsageMeter(57, "5H", s.primaryUsed, s.primaryResetsAt, s);
  drawUsageMeter(151, "7D", s.secondaryUsed, s.secondaryResetsAt, s);

  epaper.drawFastHLine(8, 266, 384, GxEPD_BLACK);
  epaper.setCursor(8, 280);
  epaper.print("AUTO REFRESH: CHANGES ONLY / MIN 30S");
  epaper.setCursor(354, 280);
  epaper.print("REV2.2");
}

void refresh(const EpaperDashboardState& s, bool full) {
  Serial.printf("[epaper] %s refresh\n", full ? "full" : "partial");
  if (full) epaper.setFullWindow();
  else epaper.setPartialWindow(0, 0, epaper.width(), epaper.height());

  epaper.firstPage();
  do {
    drawDashboard(s);
  } while (epaper.nextPage());
  epaper.powerOff();

  if (full) partialRefreshes = 0;
  else if (partialRefreshes < UINT8_MAX) ++partialRefreshes;
  rendered = true;
  lastRefreshMs = millis();
  lastSignature = signatureFor(s);
}

}  // namespace

void epaperDashboardBegin() {
  pinMode(EPD_CS, OUTPUT);
  digitalWrite(EPD_CS, HIGH);
  epaperSpi.begin(EPD_CLK, -1, EPD_DIN, EPD_CS);
  epaper.epd2.selectSPI(epaperSpi, SPISettings(4000000, MSBFIRST, SPI_MODE0));
  epaper.epd2.setBusyCallback(serviceWhileBusy);
  // Waveshare's level-shifter board uses the short reset pulse variant.
  epaper.init(0, true, 2, false);
  epaper.setRotation(0);

  epaperPet.setColorDepth(16);
  epaperPet.createSprite(PET_W, PET_H);
  ready = true;
  Serial.println("[epaper] 4.2in Rev2.2 ready");
}

void epaperDashboardPoll(const EpaperDashboardState& state, bool refreshAllowed) {
  if (!ready || !refreshAllowed) return;
  uint32_t signature = signatureFor(state);
  if (rendered && signature == lastSignature) return;

  uint32_t now = millis();
  if (rendered && (uint32_t)(now - lastRefreshMs) < MIN_REFRESH_MS) return;

  bool full = !rendered || partialRefreshes >= PARTIALS_BEFORE_FULL;
  refresh(state, full);
}

#else

void epaperDashboardBegin() {}
void epaperDashboardPoll(const EpaperDashboardState&, bool) {}

#endif
